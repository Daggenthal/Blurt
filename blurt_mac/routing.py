"""The virtual microphone on macOS.

Blurt's audio driver (macos/driver) provides two devices that are always present:
  * "Blurt-Mix" (UID blurt_mix): an output. Blurt plays your mic and your sounds into it.
  * "Blurt-Mic" (UID blurt_mic): an input that carries whatever is played into Blurt-Mix.
They use the same names as the PulseAudio sink and source Blurt creates on Linux, so the
rest of Blurt treats both systems alike.
"""
from . import audio

MIX_UID, MIC_UID = "blurt_mix", "blurt_mic"


class Router:
    """Same interface as the Linux Router (which loads PulseAudio modules)."""

    def __init__(self):
        self.ok = False
        self.message = ""
        self.loop = None
        self.loop_source = None

    def setup(self):
        uids = {d["uid"] for d in audio.devices()["devices"]}
        if MIX_UID in uids and MIC_UID in uids:
            self.ok = True
            self.message = "Select “Blurt-Mic” as the microphone in Discord / your game."
        else:
            self.ok = False
            self.message = "The Blurt-Mic driver is not installed - restart Blurt to install it."
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

    def teardown(self):
        if self.loop:
            self.loop.close()
            self.loop = None
        self.loop_source = None
