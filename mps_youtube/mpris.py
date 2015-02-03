# The MIT License (MIT)

# Copyright (c) 2015 Jiri Horner

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from threading import Thread
import json
import socket
import time
import re
import os

IDENTITY = 'mps-youtube.instance' + str(os.getpid())

BUS_NAME = 'org.mpris.MediaPlayer2.' + IDENTITY
ROOT_INTERFACE = 'org.mpris.MediaPlayer2'
PLAYER_INTERFACE = 'org.mpris.MediaPlayer2.Player'
PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
MPRIS_PATH = '/org/mpris/MediaPlayer2'

class Mpris2Controller(object):

    """
        Controller for various MPRIS objects.
    """

    def __init__(self):
        """
            Constructs an MPRIS controller. Note, you must call acquire()
        """
        self.mpris = None
        self.bus = None
        self.main_loop = GLib.MainLoop()

    def release(self):
        """
            Releases all objects from D-Bus and unregisters the bus
        """
        if self.mpris is not None:
            self.mpris.remove_from_connection()
        self.mpris = None
        if self.bus is not None:
            self.bus.get_bus().release_name(self.bus.get_name())

    def acquire(self):
        """
            Connects to D-Bus and registers all components
        """
        self._acquire_bus()
        self._add_interfaces()

    def run(self, connection):
        """
            Runs main loop, processing all calls
            binds on connection (Pipe) and listens player changes
        """
        t = Thread(target=self._run_main_loop)
        t.daemon = True
        t.start()
        self.listenstatus(connection)

    def listenstatus(self, conn):
        """
            Notifies interfaces that player connection changed
        """
        try:
            while True:
                data = conn.recv()
                if isinstance(data, tuple):
                    name, val = data
                    if name == 'socket':
                        Thread(target=self.mpris.bindmpv, args=(val,)).start()
                    elif name == 'mplayer-fifo':
                        self.mpris.bindfifo(val)
                    elif name == 'mpv-fifo':
                        self.mpris.bindfifo(val, mpv=True)
                    else:
                        self.mpris.setproperty(name, val)
        except:
            pass

    def _acquire_bus(self):
        """
            Connect to D-Bus and set self.bus to be a valid connection
        """
        if self.bus is not None:
            self.bus.get_bus().request_name(BUS_NAME)
        else:
            self.bus = dbus.service.BusName(BUS_NAME, 
                bus=dbus.SessionBus(mainloop=DBusGMainLoop()))

    def _add_interfaces(self):
        """
            Connects all interfaces to D-Bus
        """
        self.mpris = Mpris2MediaPlayer(self.bus)

    def _run_main_loop(self):
        """
            Runs glib main loop, ignoring keyboard interrupts
        """
        while True:
            try:
                self.main_loop.run()
            except KeyboardInterrupt:
                pass


class Mpris2MediaPlayer(dbus.service.Object):

    """
        implementing interfaces:
            org.mpris.MediaPlayer2
            org.mpris.MediaPlayer2.Player
    """

    def __init__(self, bus):
        dbus.service.Object.__init__(self, bus, MPRIS_PATH)
        self.socket = None
        self.fifo = None
        self.mpv = False
        self.properties = {
            ROOT_INTERFACE : {
                'read_only' : {
                    'CanQuit' : False,
                    'CanSetFullscreen' : False,
                    'CanRaise' : False,
                    'HasTrackList' : False,
                    'Identity' : IDENTITY,
                    'SupportedUriSchemes' : [],
                    'SupportedMimeTypes' : [],
                },
                'read_write' : {
                    'Fullscreen' : False,
                },
            },
            PLAYER_INTERFACE : {
                'read_only' : {
                    'PlaybackStatus' : 'Stopped',
                    'Metadata' : { 'mpris:trackid' : dbus.ObjectPath(
                                '/CurrentPlaylist/UnknownTrack', variant_level=1) },
                    'Position' : dbus.Int64(0),
                    'MinimumRate' : 1.0,
                    'MaximumRate' : 1.0,
                    'CanGoNext' : True,
                    'CanGoPrevious' : True,
                    'CanPlay' : True,
                    'CanPause' : True,
                    'CanSeek' : True,
                    'CanControl' : True,
                },
                'read_write' : {
                    'Rate' : 1.0,
                    'Volume' : 1.0,
                },
            },
        }

    def bindmpv(self, sockpath):
        self.mpv = True
        self.socket = socket.socket(socket.AF_UNIX)
        # wait on socket initialization
        tries = 0
        while tries < 10:
            time.sleep(.5)
            try:
                self.socket.connect(sockpath)
                break
            except socket.error:
                pass
            tries += 1
        else:
            self.socket = None

        try:
            observe_full = False
            self._sendcommand(["observe_property", 1, "time-pos"])

            for line in self.socket.makefile():
                resp = json.loads(line)

                # deals with race condition, when this was called too early
                if resp.get('event') == 'property-change' and not observe_full:
                    self._sendcommand(["observe_property", 2, "volume"])
                    self._sendcommand(["observe_property", 3, "pause"])
                    observe_full = True

                if resp.get('event') == 'property-change':
                    self.setproperty(resp['name'], resp['data'])

        except socket.error:
            self.socket = None
            self.mpv = False

    def bindfifo(self, fifopath, mpv=False):
        time.sleep(1) # give it some time so fifo could be properly created
        try:
            self.fifo = open(fifopath, 'w')
            self._sendcommand(['get_property', 'volume'])
            self.mpv = mpv

        except:
            self.fifo = None

    def setproperty(self, name, val):
        """
            Properly sets properties on player interface

            don't use this method from dbus interface, all values should
            be set from player (to keep them correct)
        """
        if name == 'pause':
            oldval = self.properties[PLAYER_INTERFACE]['read_only']['PlaybackStatus']
            newval = None
            if val:
                newval = 'Paused'
            else:
                newval = 'Playing'

            if newval != oldval:
                self.properties[PLAYER_INTERFACE]['read_only']['PlaybackStatus'] = newval
                self.PropertiesChanged(PLAYER_INTERFACE, { 'PlaybackStatus': newval }, [])

        elif name == 'stop':
            oldval = self.properties[PLAYER_INTERFACE]['read_only']['PlaybackStatus']
            newval = None
            if val:
                newval = 'Stopped'
            else:
                newval = 'Playing'

            if newval != oldval:
                self.properties[PLAYER_INTERFACE]['read_only']['PlaybackStatus'] = newval
                self.PropertiesChanged(PLAYER_INTERFACE, { 'PlaybackStatus': newval },
                    ['Metadata', 'Position'])

        elif name == 'volume' and val is not None:
            oldval = self.properties[PLAYER_INTERFACE]['read_write']['Volume']
            newval = float(val) / 100

            if newval != oldval:
                self.properties[PLAYER_INTERFACE]['read_write']['Volume'] = newval
                self.PropertiesChanged(PLAYER_INTERFACE, { 'Volume': newval }, [])

        elif name == 'time-pos' and val:
            oldval = self.properties[PLAYER_INTERFACE]['read_only']['Position']
            newval = dbus.Int64(val * 10**6)

            if newval != oldval:
                self.properties[PLAYER_INTERFACE]['read_only']['Position'] = newval
            if abs(newval - oldval) >= 4 * 10**6:
                self.Seeked(newval)

        elif name == 'metadata' and val:
            trackid, title, length = val
            # sanitize ytid - it uses '-_' which are not valid in dbus paths
            trackid = re.sub('[^a-zA-Z0-9]', '', trackid)

            oldval = self.properties[PLAYER_INTERFACE]['read_only']['Metadata']
            newval = {
                'mpris:trackid' : dbus.ObjectPath(
                    '/CurrentPlaylist/ytid/' + trackid, variant_level=1),
                'mpris:length' : dbus.Int64(length * 10**6, variant_level=1),
                'xesam:title' : dbus.String(title, variant_level=1) }

            if newval != oldval:
                self.properties[PLAYER_INTERFACE]['read_only']['Metadata'] = newval
                self.PropertiesChanged(PLAYER_INTERFACE, { 'Metadata': newval }, [])

    def _sendcommand(self, command):
        if self.socket:
            self.socket.send(json.dumps({"command": command}).encode() + b'\n')
        elif self.fifo:
            command = command[:]
            for x, i in enumerate(command):
                if i is True:
                    command[x] = 'yes' if self.mpv else 1
                elif i is False:
                    command[x] = 'no' if self.mpv else 0

            cmd = " ".join([str(i) for i in command]) + '\n'
            self.fifo.write(cmd)
            self.fifo.flush()

    """
        implementing org.mpris.MediaPlayer2
    """

    @dbus.service.method(dbus_interface=ROOT_INTERFACE)
    def Raise(self):
        """
            Brings the media player's user interface to the front using
            any appropriate mechanism available.
        """
        pass

    @dbus.service.method(dbus_interface=ROOT_INTERFACE)
    def Quit(self):
        """
            Causes the media player to stop running.
        """
        pass

    """
        implementing org.mpris.MediaPlayer2.Player
    """

    @dbus.service.method(dbus_interface=PLAYER_INTERFACE)
    def Next(self):
        """
            Skips to the next track in the tracklist.
        """
        self._sendcommand(["quit"])

    @dbus.service.method(PLAYER_INTERFACE)
    def Previous(self):
        """
            Skips to the previous track in the tracklist.
        """
        self._sendcommand(["quit", 42])

    @dbus.service.method(PLAYER_INTERFACE)
    def Pause(self):
        """
            Pauses playback.
            If playback is already paused, this has no effect.
        """
        if self.mpv:
            self._sendcommand(["set_property", "pause", True])
        else:
            if self.properties[PLAYER_INTERFACE]['read_only']['PlaybackStatus'] != 'Paused': 
                self._sendcommand(['pause'])

    @dbus.service.method(PLAYER_INTERFACE)
    def PlayPause(self):
        """
            Pauses playback.
            If playback is already paused, resumes playback.
        """
        if self.mpv:
            self._sendcommand(["cycle", "pause"])
        else:
            self._sendcommand(["pause"])

    @dbus.service.method(PLAYER_INTERFACE)
    def Stop(self):
        """
            Stops playback.
        """
        self._sendcommand(["quit", 43])

    @dbus.service.method(PLAYER_INTERFACE)
    def Play(self):
        """
            Starts or resumes playback.
        """
        if self.mpv:
            self._sendcommand(["set_property", "pause", False])
        else:
            if self.properties[PLAYER_INTERFACE]['read_only']['PlaybackStatus'] != 'Playing': 
                self._sendcommand(['pause'])

    @dbus.service.method(PLAYER_INTERFACE, in_signature='x')
    def Seek(self, offset):
        """
            Offset - x (offset)
                The number of microseconds to seek forward.

            Seeks forward in the current track by the specified number
            of microseconds.
        """
        self._sendcommand(["seek", offset / 10**6])

    @dbus.service.method(PLAYER_INTERFACE, in_signature='ox')
    def SetPosition(self, track_id, position):
        """
            TrackId - o (track_id)
                The currently playing track's identifier.
                If this does not match the id of the currently-playing track, the call is ignored as "stale".
            Position - x (position)
                Track position in microseconds.

            Sets the current track position in microseconds.
        """
        if track_id == self.properties[PLAYER_INTERFACE]['read_only']['Metadata']['mpris:trackid']:
            self._sendcommand(["seek", offset / 10**6, 2])

    @dbus.service.method(PLAYER_INTERFACE, in_signature='s')
    def OpenUri(self, uri):
        """
            Uri - s (uri)
                Uri of the track to load.

            Opens the Uri given as an argument.
        """
        pass

    @dbus.service.signal(PLAYER_INTERFACE, signature='x')
    def Seeked(self, position):
        """
            Position - x (position)
                The new position, in microseconds.

            Indicates that the track position has changed in a way that
            is inconsistant with the current playing state.
        """
        pass

    """
        implementing org.freedesktop.DBus.Properties
    """

    @dbus.service.method(dbus_interface=PROPERTIES_INTERFACE,
                         in_signature='ss', out_signature='v')
    def Get(self, interface_name, property_name):
        return self.GetAll(interface_name)[property_name]

    @dbus.service.method(dbus_interface=PROPERTIES_INTERFACE,
                         in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface_name):
        if interface_name in self.properties:
            t = self.properties[interface_name]['read_only'].copy()
            t.update(self.properties[interface_name]['read_write'])

            return t
        else:
            raise dbus.exceptions.DBusException(
                'com.example.UnknownInterface',
                'This object does not implement the %s interface'
                    % interface_name)

    @dbus.service.method(dbus_interface=PROPERTIES_INTERFACE,
                         in_signature='ssv')
    def Set(self, interface_name, property_name, new_value):
        if interface_name in self.properties:
            if property_name in self.properties[interface_name]['read_write']:
                if property_name == 'Volume':
                    self._sendcommand(["set_property", "volume", new_value * 100])
                    if self.fifo: # fix for mplayer (force update)
                        self._sendcommand(['get_property', 'volume'])
        else:
            raise dbus.exceptions.DBusException(
                'com.example.UnknownInterface',
                'This object does not implement the %s interface'
                    % interface_name)

    @dbus.service.signal(dbus_interface=PROPERTIES_INTERFACE,
                         signature='sa{sv}as')
    def PropertiesChanged(self, interface_name, changed_properties,
                          invalidated_properties):
        pass

def main(connection):
    conn = connection
    mprisctl = Mpris2Controller()
    mprisctl.acquire()
    mprisctl.run(connection)
    mprisctl.release()
