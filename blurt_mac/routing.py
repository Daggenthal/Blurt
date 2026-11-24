"""The virtual microphones on macOS.

Blurt's audio driver (macos/driver) provides these devices, always present:
  * "Blurt-Mix" (UID blurt_mic): a mic with your real mic + your sounds. For Discord.
  * "Blurt-Friends" (UID blurt_game_mic): the same plus the relayed app (e.g. your Discord
    friends), so people in a game hear you and them. For games.
  * two hidden outputs that Blurt plays into: the mix (UID blurt_mix) and the relay
    (UID blurt_relay). Hidden, so no app offers them by mistake.
They use the same IDs as the PulseAudio devices Blurt creates on Linux, so the rest of Blurt
treats both systems alike.
"""
import os

from . import audio

MIX_UID, MIC_UID = "blurt_mix", "blurt_mic"
RELAY_UID, GAME_MIC_UID = "blurt_relay", "blurt_game_mic"


class Router:
    """Same interface as the Linux Router (which loads PulseAudio modules)."""

    def __init__(self):
        self.ok = False
        self.message = ""
        self.loop = None
        self.loop_source = None
        self.relay = None
        self.relay_supported = False

    def setup(self):
        if audio.device_exists(MIX_UID) and audio.device_exists(MIC_UID):
            self.ok = True
            self.relay_supported = (audio.device_exists(RELAY_UID)
                                    and audio.device_exists(GAME_MIC_UID))
            self.message = ("Mic for Discord: “Blurt-Mix”. Mic for games: “Blurt-Friends”."
                            if self.relay_supported else
                            "Select “Blurt-Mix” as the microphone in Discord / your game.")
        else:
            self.ok = False
            self.message = "The Blurt-Mix driver is not installed - restart Blurt to install it."
        return self.ok

    def set_mic(self, source):
        """Feed the real microphone into the mix (None = sounds only)."""
        if source == self.loop_source and ((self.loop and self.loop.ok) or not source):
            return
        self.loop_source = source
        if self.loop:
            self.loop.close()
            self.loop = None
        if not (self.ok and source):
            return
        self.loop = audio.Loopback(source, MIX_UID, latency_ms=30)  # opens in the background

    def list_mics(self):
        return [(d["uid"], d["name"]) for d in audio.devices()["devices"]
                if d["inputs"] > 0 and not d["uid"].startswith("blurt_")]

    # ---- relay: an app's sound (e.g. Discord friends) into Blurt-Friends ----------------
    def list_apps(self):
        """Apps that play sound now: [(key, label)]."""
        me = os.getpid()
        apps = {}
        for p in audio.procs():
            if p["pid"] != me and p["app"] and p["app"] != "Blurt" and p["out"]:
                apps[p["app"].lower()] = p["app"]
        return sorted(apps.items(), key=lambda kv: kv[1].lower())

    def update_relay(self, key, gain):
        """Relay the app `key` (None = off) at volume `gain` (0..1). Called again every few
        seconds, to follow the app's processes. Returns (state, text): state is "off",
        "on", "waiting" (the app plays nothing), "loop" (the app records from
        Blurt-Friends, which would echo) or "error"."""
        if not key:
            self._close_relay()
            return "off", ""
        if not self.relay_supported:
            self._close_relay()
            return "error", "Needs the updated Blurt-Mix driver: restart Blurt to install it."
        if not audio.tap_supported():
            return "error", "Needs macOS 14.2 or later."
        me = os.getpid()
        mine = [p for p in audio.procs() if p["app"].lower() == key and p["pid"] != me]
        name = mine[0]["app"] if mine else key[:1].upper() + key[1:]
        if any(GAME_MIC_UID in p["inputs"] for p in mine):
            self._close_relay()
            return "loop", (f"Paused: {name} uses “Blurt-Friends” as its mic, so your friends "
                             f"would hear themselves. Set {name}'s mic to “Blurt-Mix”.")
        pids = tuple(sorted(p["pid"] for p in mine))
        if not pids:
            self._close_relay()
            return "waiting", f"Waiting for {name} to play sound…"
        if self.relay is None or self.relay.pids != pids or not self.relay.ok:
            if self.relay is not None and not self.relay.ok and self.relay.pids == pids:
                err = self.relay.error
                self._close_relay()
                return "error", err or "The relay stopped."
            self._close_relay()
            self.relay = audio.Relay(pids, RELAY_UID, latency_ms=30, gain=gain)
        else:
            self.relay.set_gain(gain)
        return "on", f"{name} → Blurt-Friends"

    def _close_relay(self):
        if self.relay:
            self.relay.close()
            self.relay = None

    def teardown(self):
        self._close_relay()
        if self.loop:
            self.loop.close()
            self.loop = None
        self.loop_source = None
