"""The AI voice engine, run as its own process (python -m blurt_voice.rvc.worker).

Keeping PyTorch (and ROCm/CUDA) out of the Blurt window process avoids clashes between the
graphics libraries Qt uses and the ones PyTorch brings, and if the engine crashes, Blurt
keeps running and can say why. Messages are length-prefixed pickles over stdin/stdout;
anything the libraries print goes to stderr, which Blurt saves to voice-engine.log.
"""
import faulthandler
import os
import pickle
import struct
import sys
import traceback
import warnings


def main():
    inp = sys.stdin.buffer
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # stray prints from libraries must not corrupt the message stream
    sys.stdout = sys.stderr
    faulthandler.enable(file=sys.stderr, all_threads=True)
    warnings.simplefilter("ignore", FutureWarning)

    def send(obj):
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        out.write(struct.pack("<Q", len(data)) + data)
        out.flush()

    def recv():
        head = inp.read(8)
        if len(head) < 8:
            return None
        (n,) = struct.unpack("<Q", head)
        return pickle.loads(inp.read(n))

    print(f"[engine] started, python {sys.version.split()[0]}", file=sys.stderr, flush=True)
    from . import engine
    import torch
    state = {"helpers": None, "voices": {}, "stream": None}

    def start(folder, use_gpu, fast_math, progress):
        engine.download_assets(folder, lambda name, done, total: progress(
            f"Downloading {name}" + (f" {done * 100 // total}%" if total else "")
            + f" ({done // 1048576} MB)"))
        engine.set_threads()
        progress("Starting the voice engine…")
        device = engine.pick_device(use_gpu)
        print(f"[engine] device {device} {engine.GPU_ERROR}", file=sys.stderr, flush=True)
        state["helpers"] = engine.Helpers(folder, device=device, fast_math=fast_math)
        state["voices"].clear()
        gpu_name = ""
        if device == "cuda":
            try:
                gpu_name = torch.cuda.get_device_name(0)
            except Exception:
                pass
        return {"device": device, "describe": state["helpers"].describe(),
                "gpu_error": engine.GPU_ERROR, "torch": str(torch.__version__),
                "threads": torch.get_num_threads(), "gpu_name": gpu_name}

    def load(vid, pth, index, progress):
        progress("Loading the voice… (the first time, a big .index file can take a minute to "
                 "prepare)")
        voice = engine.RVCVoice(state["helpers"], pth, index)
        progress("Measuring how fast this computer runs the voice…")
        speed = engine.benchmark(voice)
        state["voices"][vid] = voice
        info = None
        if voice.index is not None:
            info = (type(voice.index).__name__, int(voice.index.ntotal))
        return {"tgt_sr": voice.tgt_sr, "if_f0": voice.if_f0, "version": voice.version,
                "index_info": info, "speed": speed}

    def convert(vid, audio, sr, shift, index_rate, protect, progress):
        y, tsr = state["voices"][vid].convert(audio, sr, shift, index_rate, protect)
        return engine.resample(y, tsr, 48000)

    def pitch(audio, sr, progress):
        return engine.voice_pitch(state["helpers"], audio, sr)

    def stream_new(vid, block_s, context_s, shift, index_rate, protect, progress):
        s = engine.RVCStream(state["voices"][vid], block_s=block_s, context_s=context_s,
                             shift=shift, index_rate=index_rate, protect=protect)
        state["stream"] = s
        return {"block": s.block, "cross": s.cross, "context_s": s.extra16 / engine.SR}

    def stream(block, progress):
        s = state["stream"]
        y = s.process(block)
        return y, s.last_cost, s.latency(), s.report_rows(), s.silent_blocks

    def stream_set(shift, index_rate, protect, progress):
        s = state["stream"]
        if s is not None:
            s.shift, s.index_rate, s.protect = shift, index_rate, protect

    def stream_reset(progress):
        if state["stream"] is not None:
            state["stream"].stats.clear()
            state["stream"].silent_blocks = 0

    def diag(progress):
        return engine.gpu_diagnosis()

    handlers = {f.__name__: f for f in (start, load, convert, pitch, stream_new, stream,
                                        stream_set, stream_reset, diag)}
    while True:
        msg = recv()
        if msg is None:
            break
        cmd, args = msg
        try:
            result = handlers[cmd](*args, progress=lambda text: send(("progress", text)))
            send(("ok", result))
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            send(("err", f"{e}"))


if __name__ == "__main__":
    main()
