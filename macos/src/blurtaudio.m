// blurtaudio: the macOS audio and keyboard layer for Blurt, loaded from Python with ctypes.
//
// On Linux Blurt talks to PipeWire/PulseAudio with pacat/parec/pactl. On macOS this library
// provides the same building blocks natively:
//   * playback streams  (like pacat):   Python writes PCM, a Core Audio output unit plays it
//   * recording streams (like parec):   a Core Audio input unit (or a process tap) records,
//                                       the audio is resampled to the requested rate and
//                                       written into a pipe that Python reads
//   * a loopback        (like module-loopback): microphone -> Blurt-Mix, entirely native
//   * a process tap: everything the Mac plays except Blurt itself (macOS 14.2+), for clips
//   * a listen-only keyboard event tap for global hotkeys
//
// Nothing here calls back into Python, so the realtime audio threads never wait for the GIL.
// Every function that can block (writes, reads, waiting for keys) is called by ctypes, which
// releases the GIL for the duration of the call.
#import <Foundation/Foundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <CoreAudio/CoreAudio.h>
#import <ApplicationServices/ApplicationServices.h>
#include <pthread.h>
#include <stdatomic.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>

#if __MAC_OS_X_VERSION_MAX_ALLOWED >= 140200
#import <CoreAudio/AudioHardwareTapping.h>
#import <CoreAudio/CATapDescription.h>
#define HAVE_TAPS 1
#else
#define HAVE_TAPS 0
#endif

#define EXPORT __attribute__((visibility("default")))

static char g_error[512];
static void set_error(NSString *what, OSStatus st) {
    NSString *msg = st ? [NSString stringWithFormat:@"%@ (error %d)", what, (int)st] : what;
    strlcpy(g_error, msg.UTF8String ?: "error", sizeof g_error);
}

EXPORT const char *ba_last_error(void) { return g_error; }
EXPORT int ba_version(void) { return 1; }
EXPORT void ba_free(void *p) { free(p); }

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

// ---------------------------------------------------------------------------------------
// Single-producer single-consumer ring of interleaved float frames.
// ---------------------------------------------------------------------------------------
typedef struct {
    float *data;
    uint32_t cap;          // frames, a power of two
    uint32_t ch;
    _Atomic uint64_t w, r; // total frames written / read
} Ring;

static int ring_init(Ring *rb, uint32_t min_frames, uint32_t ch) {
    uint32_t cap = 1;
    while (cap < min_frames) cap <<= 1;
    rb->data = calloc((size_t)cap * ch, sizeof(float));
    rb->cap = cap;
    rb->ch = ch;
    atomic_store(&rb->w, 0);
    atomic_store(&rb->r, 0);
    return rb->data != NULL;
}

static void ring_free(Ring *rb) { free(rb->data); rb->data = NULL; }

static uint32_t ring_fill(Ring *rb) {
    return (uint32_t)(atomic_load_explicit(&rb->w, memory_order_acquire)
                      - atomic_load_explicit(&rb->r, memory_order_acquire));
}

// Producer side. Writes up to n frames, returns how many fitted.
static uint32_t ring_write(Ring *rb, const float *src, uint32_t n) {
    uint64_t w = atomic_load_explicit(&rb->w, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&rb->r, memory_order_acquire);
    uint32_t space = rb->cap - (uint32_t)(w - r);
    if (n > space) n = space;
    uint32_t at = (uint32_t)(w & (rb->cap - 1));
    uint32_t first = n < rb->cap - at ? n : rb->cap - at;
    memcpy(rb->data + (size_t)at * rb->ch, src, (size_t)first * rb->ch * sizeof(float));
    if (n > first)
        memcpy(rb->data, src + (size_t)first * rb->ch, (size_t)(n - first) * rb->ch * sizeof(float));
    atomic_store_explicit(&rb->w, w + n, memory_order_release);
    return n;
}

// Consumer side. Reads up to n frames (dst may be NULL to skip them), returns how many.
static uint32_t ring_read(Ring *rb, float *dst, uint32_t n) {
    uint64_t r = atomic_load_explicit(&rb->r, memory_order_relaxed);
    uint64_t w = atomic_load_explicit(&rb->w, memory_order_acquire);
    uint32_t have = (uint32_t)(w - r);
    if (n > have) n = have;
    if (dst) {
        uint32_t at = (uint32_t)(r & (rb->cap - 1));
        uint32_t first = n < rb->cap - at ? n : rb->cap - at;
        memcpy(dst, rb->data + (size_t)at * rb->ch, (size_t)first * rb->ch * sizeof(float));
        if (n > first)
            memcpy(dst + (size_t)first * rb->ch, rb->data, (size_t)(n - first) * rb->ch * sizeof(float));
    }
    atomic_store_explicit(&rb->r, r + n, memory_order_release);
    return n;
}

// ---------------------------------------------------------------------------------------
// Devices
// ---------------------------------------------------------------------------------------
static const AudioObjectPropertyAddress kDevicesAddr = {
    kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};

static NSString *device_string(AudioObjectID dev, AudioObjectPropertySelector sel) {
    AudioObjectPropertyAddress a = {sel, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    CFStringRef s = NULL;
    UInt32 size = sizeof s;
    if (AudioObjectGetPropertyData(dev, &a, 0, NULL, &size, &s) != noErr || !s) return @"";
    return (__bridge_transfer NSString *)s;
}

static UInt32 device_channels(AudioObjectID dev, AudioObjectPropertyScope scope) {
    AudioObjectPropertyAddress a = {kAudioDevicePropertyStreamConfiguration, scope,
                                    kAudioObjectPropertyElementMain};
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(dev, &a, 0, NULL, &size) != noErr || size == 0) return 0;
    AudioBufferList *list = malloc(size);
    UInt32 total = 0;
    if (AudioObjectGetPropertyData(dev, &a, 0, NULL, &size, list) == noErr)
        for (UInt32 i = 0; i < list->mNumberBuffers; i++) total += list->mBuffers[i].mNumberChannels;
    free(list);
    return total;
}

static Float64 device_rate(AudioObjectID dev) {
    AudioObjectPropertyAddress a = {kAudioDevicePropertyNominalSampleRate,
                                    kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    Float64 rate = 0;
    UInt32 size = sizeof rate;
    AudioObjectGetPropertyData(dev, &a, 0, NULL, &size, &rate);
    return rate;
}

static AudioObjectID default_device(int input) {
    AudioObjectPropertyAddress a = {input ? kAudioHardwarePropertyDefaultInputDevice
                                          : kAudioHardwarePropertyDefaultOutputDevice,
                                    kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    AudioObjectID dev = kAudioObjectUnknown;
    UInt32 size = sizeof dev;
    AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &size, &dev);
    return dev;
}

static AudioObjectID device_for_uid(const char *uid) {
    if (!uid || !*uid) return kAudioObjectUnknown;
    CFStringRef s = CFStringCreateWithCString(NULL, uid, kCFStringEncodingUTF8);
    AudioObjectPropertyAddress a = {kAudioHardwarePropertyTranslateUIDToDevice,
                                    kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    AudioObjectID dev = kAudioObjectUnknown;
    UInt32 size = sizeof dev;
    OSStatus st = AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, sizeof s, &s, &size, &dev);
    CFRelease(s);
    return st == noErr ? dev : kAudioObjectUnknown;
}

// JSON: {"devices": [{"uid", "name", "inputs", "outputs", "rate"}...],
//        "default_input": uid, "default_output": uid}
EXPORT char *ba_devices_json(void) {
    @autoreleasepool {
        NSMutableArray *list = [NSMutableArray array];
        UInt32 size = 0;
        if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &kDevicesAddr, 0, NULL, &size) == noErr) {
            UInt32 n = size / sizeof(AudioObjectID);
            AudioObjectID *ids = calloc(n ? n : 1, sizeof(AudioObjectID));
            if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &kDevicesAddr, 0, NULL, &size, ids) == noErr) {
                for (UInt32 i = 0; i < n; i++) {
                    [list addObject:@{
                        @"uid": device_string(ids[i], kAudioDevicePropertyDeviceUID),
                        @"name": device_string(ids[i], kAudioObjectPropertyName),
                        @"inputs": @(device_channels(ids[i], kAudioObjectPropertyScopeInput)),
                        @"outputs": @(device_channels(ids[i], kAudioObjectPropertyScopeOutput)),
                        @"rate": @(device_rate(ids[i])),
                    }];
                }
            }
            free(ids);
        }
        AudioObjectID din = default_device(1), dout = default_device(0);
        NSDictionary *all = @{
            @"devices": list,
            @"default_input": din ? device_string(din, kAudioDevicePropertyDeviceUID) : @"",
            @"default_output": dout ? device_string(dout, kAudioDevicePropertyDeviceUID) : @"",
        };
        NSData *json = [NSJSONSerialization dataWithJSONObject:all options:0 error:nil];
        char *out = calloc(json.length + 1, 1);
        memcpy(out, json.bytes, json.length);
        return out;
    }
}

// Calls `alive` = 0 when the device goes away (unplugged, driver removed...).
typedef struct {
    AudioObjectID dev;
    _Atomic int *alive;
} AliveWatch;

static OSStatus alive_changed(AudioObjectID dev, UInt32 n, const AudioObjectPropertyAddress *addrs, void *ctx) {
    (void)n; (void)addrs;
    UInt32 alive = 1, size = sizeof alive;
    AudioObjectPropertyAddress a = {kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal,
                                    kAudioObjectPropertyElementMain};
    if (AudioObjectGetPropertyData(dev, &a, 0, NULL, &size, &alive) != noErr || !alive)
        atomic_store((_Atomic int *)ctx, 0);
    return noErr;
}

static const AudioObjectPropertyAddress kAliveAddr = {
    kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};

// Ends a recording (`alive` = 0) when what it was set up for changes: the device's sample
// rate (a Bluetooth headset drops to a lower rate while its microphone is in use), or, for
// the system-audio tap, the default output. Blurt then simply opens a new recording.
static OSStatus setup_changed(AudioObjectID obj, UInt32 n, const AudioObjectPropertyAddress *addrs, void *ctx) {
    (void)obj; (void)n; (void)addrs;
    atomic_store((_Atomic int *)ctx, 0);
    return noErr;
}

static const AudioObjectPropertyAddress kRateAddr = {
    kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
static const AudioObjectPropertyAddress kDefaultOutAddr = {
    kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};

// ---------------------------------------------------------------------------------------
// Playback: Python writes interleaved float frames; the output unit plays them. The output
// unit converts sample rate and format to whatever the device uses.
// ---------------------------------------------------------------------------------------
typedef struct BAOut {
    AudioComponentInstance unit;
    AudioObjectID dev;            // 0 when following the system default output
    Ring ring;                    // always stereo
    uint32_t channels;            // what the caller writes: 1 (mono, played on both sides) or 2
    uint32_t target;              // frames kept queued at most (sets the delay)
    uint32_t prebuf;              // frames needed before playing (again)
    int playing;                  // (render thread only) 0 while prebuffering
    _Atomic int alive;
    _Atomic int closing;
    _Atomic uint64_t underruns;
    float *conv;                  // mono -> stereo scratch
    uint32_t conv_frames;
} BAOut;

static OSStatus out_render(void *ctx, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts,
                           UInt32 bus, UInt32 frames, AudioBufferList *io) {
    (void)ts; (void)bus;
    BAOut *o = ctx;
    float *dst = io->mBuffers[0].mData;
    // Like PulseAudio's prebuffering: after starting, or running dry, wait until some audio
    // is queued, so a writer that delivers in bursts (a microphone) gets a steady margin.
    if (!o->playing && ring_fill(&o->ring) >= o->prebuf) o->playing = 1;
    uint32_t got = o->playing ? ring_read(&o->ring, dst, frames) : 0;
    if (got < frames) {
        memset(dst + (size_t)got * 2, 0, (size_t)(frames - got) * 2 * sizeof(float));
        if (o->playing) {
            o->playing = 0;
            if (got) atomic_fetch_add(&o->underruns, 1);
        }
        if (got == 0) *flags |= kAudioUnitRenderAction_OutputIsSilence;
    }
    return noErr;
}

EXPORT void ba_out_close(BAOut *o);

// uid: device UID, or NULL/"" for the system default output (followed when it changes).
EXPORT BAOut *ba_out_open(const char *uid, double rate, int channels, double latency_ms) {
    BAOut *o = calloc(1, sizeof *o);
    o->channels = channels == 1 ? 1 : 2;
    atomic_store(&o->alive, 1);
    int use_default = !uid || !*uid;
    if (!use_default) {
        o->dev = device_for_uid(uid);
        if (!o->dev) {
            set_error([NSString stringWithFormat:@"Output device not found: %s", uid], 0);
            free(o);
            return NULL;
        }
    }
    AudioComponentDescription desc = {kAudioUnitType_Output,
                                      use_default ? kAudioUnitSubType_DefaultOutput : kAudioUnitSubType_HALOutput,
                                      kAudioUnitManufacturer_Apple, 0, 0};
    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    OSStatus st = comp ? AudioComponentInstanceNew(comp, &o->unit) : -1;
    if (st) { set_error(@"Could not create an audio output", st); free(o); return NULL; }
    if (!use_default) {
        st = AudioUnitSetProperty(o->unit, kAudioOutputUnitProperty_CurrentDevice,
                                  kAudioUnitScope_Global, 0, &o->dev, sizeof o->dev);
        if (st) { set_error(@"Could not select the output device", st); goto fail; }
    }
    AudioStreamBasicDescription fmt = {
        .mSampleRate = rate, .mFormatID = kAudioFormatLinearPCM,
        .mFormatFlags = kAudioFormatFlagsNativeFloatPacked,
        .mBytesPerPacket = 8, .mFramesPerPacket = 1, .mBytesPerFrame = 8,
        .mChannelsPerFrame = 2, .mBitsPerChannel = 32};
    st = AudioUnitSetProperty(o->unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0,
                              &fmt, sizeof fmt);
    if (st) { set_error(@"The output device does not accept the audio format", st); goto fail; }
    UInt32 io_frames = 256;  // about 5 ms: a hotkey starts a sound with very little delay
    AudioUnitSetProperty(o->unit, kAudioDevicePropertyBufferFrameSize, kAudioUnitScope_Global, 0,
                         &io_frames, sizeof io_frames);
    UInt32 size = sizeof io_frames;
    AudioUnitGetProperty(o->unit, kAudioDevicePropertyBufferFrameSize, kAudioUnitScope_Global, 0,
                         &io_frames, &size);
    // the client side runs at `rate`; the device buffer is in device frames
    Float64 drate = o->dev ? device_rate(o->dev) : 0;
    double io_client = drate > 0 ? io_frames * rate / drate : io_frames;
    o->target = (uint32_t)(latency_ms * rate / 1000.0 + 2 * io_client);
    o->prebuf = o->target / 2;
    if (!ring_init(&o->ring, (uint32_t)rate, 2)) { set_error(@"Out of memory", 0); goto fail; }
    AURenderCallbackStruct cb = {out_render, o};
    st = AudioUnitSetProperty(o->unit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0,
                              &cb, sizeof cb);
    if (!st) st = AudioUnitInitialize(o->unit);
    if (!st) st = AudioOutputUnitStart(o->unit);
    if (st) { set_error(@"Could not start the audio output", st); goto fail; }
    if (o->dev) AudioObjectAddPropertyListener(o->dev, &kAliveAddr, alive_changed, &o->alive);
    return o;
fail:
    if (o->unit) AudioComponentInstanceDispose(o->unit);
    ring_free(&o->ring);
    free(o);
    return NULL;
}

// Blocks until all `frames` frames are queued (keeping at most the target queued). Returns
// the number of frames written, or -1 once the stream is dead (device gone or closed).
EXPORT int ba_out_write(BAOut *o, const float *data, int frames) {
    if (!o) return -1;
    const float *src = data;
    if (o->channels == 1) {
        if ((uint32_t)frames > o->conv_frames) {
            free(o->conv);
            o->conv = malloc((size_t)frames * 2 * sizeof(float));
            o->conv_frames = frames;
        }
        for (int i = 0; i < frames; i++) o->conv[2 * i] = o->conv[2 * i + 1] = data[i];
        src = o->conv;
    }
    int done = 0;
    while (done < frames) {
        if (!atomic_load(&o->alive) || atomic_load(&o->closing)) return -1;
        uint32_t fill = ring_fill(&o->ring);
        if (fill < o->target) {
            uint32_t n = o->target - fill;
            if (n > (uint32_t)(frames - done)) n = frames - done;
            done += ring_write(&o->ring, src + (size_t)done * 2, n);
        } else {
            usleep(1000);
        }
    }
    return done;
}

EXPORT int ba_out_queued(BAOut *o) { return o ? (int)ring_fill(&o->ring) : 0; }

// Makes a blocked ba_out_write return (-1) without freeing anything; call ba_out_close after.
EXPORT void ba_out_abort(BAOut *o) { if (o) atomic_store(&o->closing, 1); }

EXPORT void ba_out_close(BAOut *o) {
    if (!o) return;
    atomic_store(&o->closing, 1);
    if (o->dev) AudioObjectRemovePropertyListener(o->dev, &kAliveAddr, alive_changed, &o->alive);
    AudioOutputUnitStop(o->unit);
    AudioUnitUninitialize(o->unit);
    AudioComponentInstanceDispose(o->unit);
    ring_free(&o->ring);
    free(o->conv);
    free(o);
}

// ---------------------------------------------------------------------------------------
// Recording: a capture source (a device, or the process tap) fills a ring at the device's
// own rate; a reader converts it to the requested rate and channel count.
// ---------------------------------------------------------------------------------------
typedef struct BAIn {
    // source
    AudioComponentInstance unit;   // device input (AUHAL)
    AudioObjectID dev;
    AudioObjectID tap, agg;        // process tap + its private aggregate device
    AudioDeviceIOProcID proc;
    AudioBufferList *buf;          // render scratch (device channels, interleaved)
    uint32_t buf_frames;
    uint32_t src_ch;               // channels the source delivers
    uint32_t src_interleaved;
    Float64 src_rate;
    // delivered format
    uint32_t ch;                   // 1 or 2
    Float64 rate;
    Ring ring;                     // `ch` channels at src_rate
    AudioConverterRef conv;
    float *mix;                    // downmix scratch
    uint32_t mix_frames;
    _Atomic int alive;
    _Atomic int closing;
    double max_backlog_s;          // older audio is skipped beyond this
    // pipe writer
    pthread_t thread;
    int has_thread;
    int fd;
    int s16;                       // deliver 16-bit integers instead of floats
    pid_t exclude_pid;             // tap: retry excluding this process if it had no audio yet
    int excluded;
    double next_retry;
    AudioObjectID rate_dev;        // the device whose sample rate src_rate is (watched)
    int watch_default_out;         // tap: also watching the default output
} BAIn;

// Push `frames` frames of `n_ch`-channel audio (interleaved, or one buffer per channel) into
// the ring as the stream's channel count. Runs on the realtime thread: no locks, no malloc.
static void in_push(BAIn *in, const AudioBufferList *list, uint32_t frames) {
    if (frames > in->mix_frames) frames = in->mix_frames;
    float *dst = in->mix;
    uint32_t want = in->ch;
    if (list->mNumberBuffers == 1) {
        uint32_t n = list->mBuffers[0].mNumberChannels;
        const float *s = list->mBuffers[0].mData;
        if (!s || n == 0) return;
        for (uint32_t i = 0; i < frames; i++, s += n) {
            if (want == 1) {
                float sum = 0;
                for (uint32_t c = 0; c < n; c++) sum += s[c];
                dst[i] = sum / n;
            } else {
                dst[2 * i] = s[0];
                dst[2 * i + 1] = n > 1 ? s[1] : s[0];
            }
        }
    } else {  // non-interleaved: one buffer per channel
        uint32_t n = list->mNumberBuffers;
        for (uint32_t i = 0; i < frames; i++) {
            if (want == 1) {
                float sum = 0;
                for (uint32_t c = 0; c < n; c++) sum += ((const float *)list->mBuffers[c].mData)[i];
                dst[i] = sum / n;
            } else {
                dst[2 * i] = ((const float *)list->mBuffers[0].mData)[i];
                dst[2 * i + 1] = ((const float *)list->mBuffers[n > 1 ? 1 : 0].mData)[i];
            }
        }
    }
    ring_write(&in->ring, dst, frames);  // a full ring means the reader stalled: drop the new
}

static OSStatus in_render(void *ctx, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts,
                          UInt32 bus, UInt32 frames, AudioBufferList *unused) {
    (void)unused;
    BAIn *in = ctx;
    if (frames > in->buf_frames) return noErr;
    in->buf->mBuffers[0].mDataByteSize = frames * in->src_ch * sizeof(float);
    OSStatus st = AudioUnitRender(in->unit, flags, ts, bus, frames, in->buf);
    if (st == noErr) in_push(in, in->buf, frames);
    return noErr;
}

// Feeds the converter from the ring, waiting (this is the reader thread) until audio comes.
static OSStatus conv_input(AudioConverterRef c, UInt32 *packets, AudioBufferList *data,
                           AudioStreamPacketDescription **desc, void *ctx) {
    (void)c; (void)desc;
    BAIn *in = ctx;
    static __thread float *scratch;
    static __thread uint32_t scratch_frames;
    uint32_t want = *packets;
    if (want > 4096) want = 4096;
    while (ring_fill(&in->ring) == 0) {
        if (atomic_load(&in->closing) || !atomic_load(&in->alive)) {
            *packets = 0;
            return 1;  // "no more data": the converter returns what it has
        }
        usleep(1000);
    }
    if (want > scratch_frames) {
        free(scratch);
        scratch = malloc((size_t)want * 2 * sizeof(float));
        scratch_frames = want;
    }
    uint32_t got = ring_read(&in->ring, scratch, want);
    data->mNumberBuffers = 1;
    data->mBuffers[0].mData = scratch;
    data->mBuffers[0].mNumberChannels = in->ch;
    data->mBuffers[0].mDataByteSize = got * in->ch * sizeof(float);
    *packets = got;
    return noErr;
}

#if HAVE_TAPS
static void tap_retry_exclude(BAIn *in);
#endif

// Reads converted audio: blocks until `frames` frames are ready, or the stream ends.
static int in_read(BAIn *in, float *out, int frames) {
    // skip a backlog (e.g. after the reader was blocked) so the delay stays short
    uint32_t backlog = ring_fill(&in->ring);
    uint32_t keep = (uint32_t)(0.02 * in->src_rate);
    if (backlog > in->max_backlog_s * in->src_rate) ring_read(&in->ring, NULL, backlog - keep);
    if (!in->conv) {
        int done = 0;
        while (done < frames) {
            if (atomic_load(&in->closing) || !atomic_load(&in->alive)) return done;
            uint32_t got = ring_read(&in->ring, out + (size_t)done * in->ch, frames - done);
            done += got;
            if (!got) usleep(1000);
        }
        return done;
    }
    AudioBufferList list = {1, {{in->ch, (UInt32)(frames * in->ch * sizeof(float)), out}}};
    UInt32 n = frames;
    AudioConverterFillComplexBuffer(in->conv, conv_input, in, &n, &list, NULL);
    return (int)n;
}

static void *in_pipe_thread(void *ctx) {
    BAIn *in = ctx;
    pthread_setname_np("Blurt recording");
    uint32_t chunk = (uint32_t)(in->rate / 100);  // 10 ms pieces
    float *buf = malloc((size_t)chunk * in->ch * sizeof(float));
    int16_t *s16 = malloc((size_t)chunk * in->ch * sizeof(int16_t));
    while (!atomic_load(&in->closing)) {
#if HAVE_TAPS
        if (in->tap && !in->excluded && now_s() > in->next_retry) tap_retry_exclude(in);
#endif
        int got = in_read(in, buf, chunk);
        if (got <= 0) break;
        const char *data = (const char *)buf;
        size_t bytes = (size_t)got * in->ch * sizeof(float), off = 0;
        if (in->s16) {
            for (size_t i = 0; i < (size_t)got * in->ch; i++) {
                float v = buf[i] * 32768.0f;
                s16[i] = (int16_t)(v > 32767.0f ? 32767 : v < -32768.0f ? -32768 : lrintf(v));
            }
            data = (const char *)s16;
            bytes = (size_t)got * in->ch * sizeof(int16_t);
        }
        while (off < bytes) {  // the pipe is non-blocking, so closing never waits on Python
            ssize_t w = write(in->fd, data + off, bytes - off);
            if (w < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN) {
                    if (atomic_load(&in->closing)) goto done;
                    usleep(2000);
                    continue;
                }
                goto done;  // Python closed its end
            }
            off += w;
        }
    }
done:
    free(buf);
    free(s16);
    close(in->fd);  // Python sees end-of-file
    in->fd = -1;
    return NULL;
}

static int in_finish_setup(BAIn *in, double max_backlog_s) {
    in->max_backlog_s = max_backlog_s;
    in->mix_frames = 16384;
    in->mix = malloc((size_t)in->mix_frames * 2 * sizeof(float));
    if (!ring_init(&in->ring, (uint32_t)(in->src_rate * 1.5), in->ch)) return 0;
    if (fabs(in->src_rate - in->rate) > 0.5) {
        AudioStreamBasicDescription a = {
            .mSampleRate = in->src_rate, .mFormatID = kAudioFormatLinearPCM,
            .mFormatFlags = kAudioFormatFlagsNativeFloatPacked,
            .mBytesPerPacket = 4 * in->ch, .mFramesPerPacket = 1, .mBytesPerFrame = 4 * in->ch,
            .mChannelsPerFrame = in->ch, .mBitsPerChannel = 32};
        AudioStreamBasicDescription b = a;
        b.mSampleRate = in->rate;
        if (AudioConverterNew(&a, &b, &in->conv) != noErr) return 0;
        UInt32 q = kAudioConverterQuality_High;
        AudioConverterSetProperty(in->conv, kAudioConverterSampleRateConverterQuality, sizeof q, &q);
    }
    return 1;
}

EXPORT void ba_in_close(BAIn *in);

// Starts the thread that writes the recorded audio into a pipe: the write end `wfd` the
// caller made (returns 0), or, with wfd = -1, a new pipe whose read end it returns. -1 on
// failure. The stream owns wfd from here on (ba_in_close closes it).
static int in_start_pipe(BAIn *in, int wfd) {
    int rfd = -1;
    if (wfd < 0) {
        int fds[2];
        if (pipe(fds) != 0) return -1;
        fcntl(fds[0], F_SETFD, FD_CLOEXEC);
        rfd = fds[0];
        wfd = fds[1];
    }
    fcntl(wfd, F_SETFD, FD_CLOEXEC);
    fcntl(wfd, F_SETFL, fcntl(wfd, F_GETFL) | O_NONBLOCK);
    signal(SIGPIPE, SIG_IGN);  // a closed pipe is reported by write() instead
    in->fd = wfd;
    if (pthread_create(&in->thread, NULL, in_pipe_thread, in) != 0) {
        if (rfd >= 0) close(rfd);
        return -1;
    }
    in->has_thread = 1;
    return rfd >= 0 ? rfd : 0;
}

static BAIn *in_open_device(const char *uid, double rate, int channels) {
    BAIn *in = calloc(1, sizeof *in);
    in->fd = -1;
    in->ch = channels == 1 ? 1 : 2;
    in->rate = rate;
    atomic_store(&in->alive, 1);
    in->dev = (uid && *uid) ? device_for_uid(uid) : default_device(1);
    if (!in->dev) {
        set_error([NSString stringWithFormat:@"Input device not found: %s", uid ?: "(default)"], 0);
        free(in);
        return NULL;
    }
    in->src_ch = device_channels(in->dev, kAudioObjectPropertyScopeInput);
    in->src_rate = device_rate(in->dev);
    if (in->src_ch == 0 || in->src_rate <= 0) {
        set_error(@"That device has no microphone input", 0);
        free(in);
        return NULL;
    }
    if (in->src_ch > 64) in->src_ch = 64;
    AudioComponentDescription desc = {kAudioUnitType_Output, kAudioUnitSubType_HALOutput,
                                      kAudioUnitManufacturer_Apple, 0, 0};
    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    OSStatus st = comp ? AudioComponentInstanceNew(comp, &in->unit) : -1;
    if (st) { set_error(@"Could not create an audio input", st); free(in); return NULL; }
    UInt32 on = 1, off = 0;
    AudioUnitSetProperty(in->unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &on, sizeof on);
    AudioUnitSetProperty(in->unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &off, sizeof off);
    st = AudioUnitSetProperty(in->unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                              &in->dev, sizeof in->dev);
    if (st) { set_error(@"Could not select the microphone", st); goto fail; }
    // capture at the device's own rate and channel count; we convert afterwards
    AudioStreamBasicDescription fmt = {
        .mSampleRate = in->src_rate, .mFormatID = kAudioFormatLinearPCM,
        .mFormatFlags = kAudioFormatFlagsNativeFloatPacked,
        .mBytesPerPacket = 4 * in->src_ch, .mFramesPerPacket = 1, .mBytesPerFrame = 4 * in->src_ch,
        .mChannelsPerFrame = in->src_ch, .mBitsPerChannel = 32};
    st = AudioUnitSetProperty(in->unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1,
                              &fmt, sizeof fmt);
    if (st) { set_error(@"The microphone does not accept the audio format", st); goto fail; }
    UInt32 io_frames = 256;
    AudioUnitSetProperty(in->unit, kAudioDevicePropertyBufferFrameSize, kAudioUnitScope_Global, 0,
                         &io_frames, sizeof io_frames);
    in->buf_frames = 8192;
    UInt32 max = in->buf_frames;
    AudioUnitSetProperty(in->unit, kAudioUnitProperty_MaximumFramesPerSlice, kAudioUnitScope_Global, 0,
                         &max, sizeof max);
    in->buf = calloc(1, sizeof(AudioBufferList));
    in->buf->mNumberBuffers = 1;
    in->buf->mBuffers[0].mNumberChannels = in->src_ch;
    in->buf->mBuffers[0].mData = calloc((size_t)in->buf_frames * in->src_ch, sizeof(float));
    if (!in_finish_setup(in, 0.25)) { set_error(@"Could not set up audio conversion", 0); goto fail; }
    AURenderCallbackStruct cb = {in_render, in};
    st = AudioUnitSetProperty(in->unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0,
                              &cb, sizeof cb);
    if (!st) st = AudioUnitInitialize(in->unit);
    if (!st) st = AudioOutputUnitStart(in->unit);
    if (st) { set_error(@"Could not start recording from the microphone", st); goto fail; }
    AudioObjectAddPropertyListener(in->dev, &kAliveAddr, alive_changed, &in->alive);
    if (AudioObjectAddPropertyListener(in->dev, &kRateAddr, setup_changed, &in->alive) == noErr)
        in->rate_dev = in->dev;
    return in;
fail:
    ba_in_close(in);
    return NULL;
}

// Records from a device (uid NULL/"" = the default input) into a pipe, as interleaved audio
// at `rate` (format 0: float32, 1: int16); the stream handle goes to *out. With wfd = -1 it
// makes the pipe and returns its read end; otherwise it writes into wfd (which it owns from
// now on, even on failure) and returns 0. -1 on failure.
// This can block for as long as macOS shows its microphone permission prompt, so Blurt
// calls it from a background thread.
EXPORT int ba_in_open(const char *uid, double rate, int channels, int format, int wfd, BAIn **out) {
    *out = NULL;
    BAIn *in = in_open_device(uid, rate, channels);
    if (!in) {
        if (wfd >= 0) close(wfd);
        return -1;
    }
    in->s16 = format == 1;
    int fd = in_start_pipe(in, wfd);
    if (fd < 0) { set_error(@"Could not start the recording thread", 0); ba_in_close(in); return -1; }
    *out = in;
    return fd;
}

// ---------------------------------------------------------------------------------------
// Process tap: everything the Mac plays, except one process (Blurt). macOS 14.2 and later.
// ---------------------------------------------------------------------------------------
EXPORT int ba_tap_supported(void) {
#if HAVE_TAPS
    if (@available(macOS 14.2, *)) return 1;
#endif
    return 0;
}

#if HAVE_TAPS
static AudioObjectID process_object(pid_t pid) API_AVAILABLE(macos(14.2)) {
    AudioObjectPropertyAddress a = {kAudioHardwarePropertyTranslatePIDToProcessObject,
                                    kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    AudioObjectID obj = kAudioObjectUnknown;
    UInt32 size = sizeof obj;
    AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, sizeof pid, &pid, &size, &obj);
    return obj;
}

// Blurt may not have played anything yet when the tap starts, and a process only gets an
// audio object once it does; until then, keep trying to exclude it.
static void tap_retry_exclude(BAIn *in) API_AVAILABLE(macos(14.2)) {
    in->next_retry = now_s() + 1.0;
    AudioObjectID obj = process_object(in->exclude_pid);
    if (obj == kAudioObjectUnknown) return;
    @autoreleasepool {
        AudioObjectPropertyAddress a = {kAudioTapPropertyDescription, kAudioObjectPropertyScopeGlobal,
                                        kAudioObjectPropertyElementMain};
        CATapDescription *desc = nil;
        UInt32 size = sizeof desc;
        CFTypeRef ref = NULL;
        if (AudioObjectGetPropertyData(in->tap, &a, 0, NULL, &size, &ref) != noErr || !ref) return;
        desc = (__bridge_transfer CATapDescription *)ref;
        desc.processes = @[@(obj)];
        ref = (__bridge CFTypeRef)desc;
        if (AudioObjectSetPropertyData(in->tap, &a, 0, NULL, sizeof ref, &ref) == noErr) in->excluded = 1;
    }
}

static OSStatus tap_io(AudioObjectID dev, const AudioTimeStamp *now, const AudioBufferList *input,
                       const AudioTimeStamp *input_time, AudioBufferList *output,
                       const AudioTimeStamp *output_time, void *ctx) {
    (void)dev; (void)now; (void)input_time; (void)output; (void)output_time;
    BAIn *in = ctx;
    if (!input || input->mNumberBuffers == 0) return noErr;
    // stereo tap: one interleaved buffer of 2 channels, or two mono buffers
    uint32_t frames = input->mBuffers[0].mDataByteSize /
                      (sizeof(float) * (input->mNumberBuffers == 1 ? input->mBuffers[0].mNumberChannels : 1));
    in_push(in, input, frames);
    return noErr;
}
#endif

// Records what the Mac plays (all outputs mixed), leaving out process `exclude_pid`. Pipe
// and return value as for ba_in_open; this blocks while macOS asks for permission.
EXPORT int ba_tap_open(int exclude_pid, double rate, int channels, int format, int wfd, BAIn **out) {
    *out = NULL;
#if HAVE_TAPS
    if (@available(macOS 14.2, *)) {
        BAIn *in = calloc(1, sizeof *in);
        in->fd = wfd;  // owned by the stream from here: ba_in_close closes it on failure
        in->ch = channels == 1 ? 1 : 2;
        in->rate = rate;
        in->exclude_pid = exclude_pid;
        in->s16 = format == 1;
        atomic_store(&in->alive, 1);
        OSStatus st;
        @autoreleasepool {
            AudioObjectID me = exclude_pid > 0 ? process_object(exclude_pid) : kAudioObjectUnknown;
            NSArray *exclude = me != kAudioObjectUnknown ? @[@(me)] : @[];
            in->excluded = me != kAudioObjectUnknown || exclude_pid <= 0;
            CATapDescription *desc = [[CATapDescription alloc] initStereoGlobalTapButExcludeProcesses:exclude];
            desc.name = @"Blurt clip buffer";
            desc.privateTap = YES;
            desc.muteBehavior = CATapUnmuted;
            st = AudioHardwareCreateProcessTap(desc, &in->tap);
            if (st) { set_error(@"Could not record what the Mac plays", st); ba_in_close(in); return -1; }
            AudioObjectPropertyAddress fa = {kAudioTapPropertyFormat, kAudioObjectPropertyScopeGlobal,
                                             kAudioObjectPropertyElementMain};
            AudioStreamBasicDescription tf = {0};
            UInt32 size = sizeof tf;
            AudioObjectGetPropertyData(in->tap, &fa, 0, NULL, &size, &tf);
            in->src_rate = tf.mSampleRate > 0 ? tf.mSampleRate : 48000;
            in->src_ch = tf.mChannelsPerFrame ? tf.mChannelsPerFrame : 2;
            AudioObjectID out_dev = default_device(0);
            NSString *out_uid = out_dev ? device_string(out_dev, kAudioDevicePropertyDeviceUID) : nil;
            NSMutableDictionary *agg = [@{
                @kAudioAggregateDeviceNameKey: @"Blurt clip buffer",
                @kAudioAggregateDeviceUIDKey: [NSUUID UUID].UUIDString,
                @kAudioAggregateDeviceIsPrivateKey: @YES,
                @kAudioAggregateDeviceIsStackedKey: @NO,
                @kAudioAggregateDeviceTapAutoStartKey: @YES,
                @kAudioAggregateDeviceTapListKey: @[@{
                    @kAudioSubTapDriftCompensationKey: @YES,
                    @kAudioSubTapUIDKey: desc.UUID.UUIDString,
                }],
            } mutableCopy];
            if (out_uid.length) {
                agg[@kAudioAggregateDeviceMainSubDeviceKey] = out_uid;
                agg[@kAudioAggregateDeviceSubDeviceListKey] = @[@{@kAudioSubDeviceUIDKey: out_uid}];
            }
            st = AudioHardwareCreateAggregateDevice((__bridge CFDictionaryRef)agg, &in->agg);
        }
        if (st) { set_error(@"Could not create the clip recorder", st); ba_in_close(in); return -1; }
        // The tap's audio arrives at the aggregate device's rate, which follows the output
        // device (44.1 kHz for many headphones), not necessarily the tap's own format.
        Float64 agg_rate = device_rate(in->agg);
        if (agg_rate > 0) in->src_rate = agg_rate;
        if (AudioObjectAddPropertyListener(in->agg, &kRateAddr, setup_changed, &in->alive) == noErr)
            in->rate_dev = in->agg;
        AudioObjectPropertyAddress sys = kDefaultOutAddr;
        if (AudioObjectAddPropertyListener(kAudioObjectSystemObject, &sys, setup_changed, &in->alive) == noErr)
            in->watch_default_out = 1;
        if (!in_finish_setup(in, 0.5)) { set_error(@"Could not set up audio conversion", 0); ba_in_close(in); return -1; }
        st = AudioDeviceCreateIOProcID(in->agg, tap_io, in, &in->proc);
        if (!st) st = AudioDeviceStart(in->agg, in->proc);
        if (st) { set_error(@"Could not start the clip recorder", st); ba_in_close(in); return -1; }
        int fd = in_start_pipe(in, wfd);
        if (fd < 0) { set_error(@"Could not start the recording thread", 0); ba_in_close(in); return -1; }
        *out = in;
        return fd;
    }
#endif
    (void)exclude_pid; (void)rate; (void)channels; (void)format;
    if (wfd >= 0) close(wfd);
    set_error(@"Recording what the Mac plays needs macOS 14.2 or later", 0);
    return -1;
}

// 1 while the recording runs; 0 once it has ended by itself (device gone or changed).
EXPORT int ba_in_running(BAIn *in) {
    return in && atomic_load(&in->alive) && !atomic_load(&in->closing);
}

// Stops recording. The pipe writer finishes and closes its end, so Python reads EOF.
EXPORT void ba_in_close(BAIn *in) {
    if (!in) return;
    atomic_store(&in->closing, 1);
    if (in->unit) {
        AudioOutputUnitStop(in->unit);
        if (in->dev) AudioObjectRemovePropertyListener(in->dev, &kAliveAddr, alive_changed, &in->alive);
    }
    if (in->agg && in->proc) {
        AudioDeviceStop(in->agg, in->proc);
        AudioDeviceDestroyIOProcID(in->agg, in->proc);
    }
    if (in->rate_dev) AudioObjectRemovePropertyListener(in->rate_dev, &kRateAddr, setup_changed, &in->alive);
    if (in->watch_default_out)
        AudioObjectRemovePropertyListener(kAudioObjectSystemObject, &kDefaultOutAddr, setup_changed, &in->alive);
    if (in->has_thread) pthread_join(in->thread, NULL);  // ends: `closing` stops its waits
    if (in->unit) {
        AudioUnitUninitialize(in->unit);
        AudioComponentInstanceDispose(in->unit);
    }
#if HAVE_TAPS
    if (@available(macOS 14.2, *)) {
        if (in->agg) AudioHardwareDestroyAggregateDevice(in->agg);
        if (in->tap) AudioHardwareDestroyProcessTap(in->tap);
    }
#endif
    if (in->conv) AudioConverterDispose(in->conv);
    if (in->fd >= 0) close(in->fd);
    if (in->buf) { free(in->buf->mBuffers[0].mData); free(in->buf); }
    ring_free(&in->ring);
    free(in->mix);
    free(in);
}

// ---------------------------------------------------------------------------------------
// Loopback: a microphone played into an output (Blurt-Mix), like PulseAudio's
// module-loopback. The two devices run on different clocks, so the delay is kept steady by
// occasionally repeating or dropping a single frame (inaudible). If the microphone's sample
// rate changes or it goes away, the microphone is opened again.
// ---------------------------------------------------------------------------------------
typedef struct BALoop {
    BAIn *in;                     // replaced on reopening, under `lock`
    BAOut *out;
    char in_uid[512];
    pthread_mutex_t lock;
    pthread_t thread;
    _Atomic int stop;
} BALoop;

// Plays the microphone into the output until the microphone stream ends (0) or the output
// does or the loopback is closed (-1).
static int loop_run(BALoop *lp) {
    enum { chunk = 240 };  // 5 ms at 48 kHz
    float buf[(chunk + 1) * 2];
    // The delay (audio waiting in both rings) is smoothed over a few seconds, its normal
    // level is learnt at the start, and only a lasting change of more than 2.5 ms is
    // corrected, by at most 10 frames a second (200 ppm, more than any real clock drift).
    const double ratio = 48000.0 / lp->in->src_rate;
    double avg = -1, normal = -1, last_fix = 0, start = now_s();
    while (!atomic_load(&lp->stop)) {
        int got = in_read(lp->in, buf, chunk);
        if (got <= 0) return 0;
        double delay = ring_fill(&lp->out->ring) + ring_fill(&lp->in->ring) * ratio;
        avg = avg < 0 ? delay : avg + (delay - avg) * 0.002;
        double now = now_s();
        if (normal < 0 && now - start > 2.0) normal = avg;
        if (normal >= 0 && now - last_fix > 0.1) {
            if (avg < normal - 120) {           // the output runs faster: repeat a frame
                memcpy(buf + got * 2, buf + (got - 1) * 2, 2 * sizeof(float));
                got++;
                last_fix = now;
            } else if (avg > normal + 120 && got > 1) {  // the input runs faster: drop one
                got--;
                last_fix = now;
            }
        }
        if (ba_out_write(lp->out, buf, got) < 0) return -1;
    }
    return -1;
}

static void *loop_thread(void *ctx) {
    BALoop *lp = ctx;
    pthread_setname_np("Blurt loopback");
    while (loop_run(lp) == 0) {
        pthread_mutex_lock(&lp->lock);
        BAIn *old = lp->in;
        lp->in = NULL;
        pthread_mutex_unlock(&lp->lock);
        ba_in_close(old);
        BAIn *in = NULL;
        while (!in) {  // try again until the microphone is back (or Blurt stops the loopback)
            for (int i = 0; i < 5 && !atomic_load(&lp->stop); i++) usleep(100000);
            if (atomic_load(&lp->stop)) return NULL;
            in = in_open_device(lp->in_uid, 48000, 2);
        }
        pthread_mutex_lock(&lp->lock);
        lp->in = in;
        if (atomic_load(&lp->stop)) atomic_store(&in->closing, 1);
        pthread_mutex_unlock(&lp->lock);
    }
    return NULL;
}

EXPORT void *ba_loop_open(const char *in_uid, const char *out_uid, double latency_ms) {
    BALoop *lp = calloc(1, sizeof *lp);
    lp->in = in_open_device(in_uid, 48000, 2);
    if (!lp->in) { free(lp); return NULL; }
    strlcpy(lp->in_uid, in_uid ? in_uid : "", sizeof lp->in_uid);
    pthread_mutex_init(&lp->lock, NULL);
    lp->out = ba_out_open(out_uid, 48000, 2, latency_ms);
    if (!lp->out) { ba_in_close(lp->in); pthread_mutex_destroy(&lp->lock); free(lp); return NULL; }
    if (pthread_create(&lp->thread, NULL, loop_thread, lp) != 0) {
        ba_out_close(lp->out);
        ba_in_close(lp->in);
        pthread_mutex_destroy(&lp->lock);
        free(lp);
        set_error(@"Could not start the loopback thread", 0);
        return NULL;
    }
    return lp;
}

EXPORT void ba_loop_close(void *p) {
    BALoop *lp = p;
    if (!lp) return;
    atomic_store(&lp->stop, 1);
    pthread_mutex_lock(&lp->lock);
    if (lp->in) atomic_store(&lp->in->closing, 1);  // wakes in_read
    pthread_mutex_unlock(&lp->lock);
    atomic_store(&lp->out->closing, 1);  // wakes ba_out_write
    pthread_join(lp->thread, NULL);
    ba_in_close(lp->in);
    ba_out_close(lp->out);
    pthread_mutex_destroy(&lp->lock);
    free(lp);
}

// ---------------------------------------------------------------------------------------
// Global hotkeys: a listen-only event tap. Keys are observed, never swallowed. Needs the
// Input Monitoring permission (System Settings > Privacy & Security).
// ---------------------------------------------------------------------------------------
#define KEYQ 64
static struct {
    pthread_mutex_t lock;
    pthread_cond_t cond;
    uint32_t codes[KEYQ];
    uint64_t flags[KEYQ];
    uint32_t head, count;
    CFMachPortRef tap;
    int state;  // 0 starting, 1 running, -1 failed
} K = {.lock = PTHREAD_MUTEX_INITIALIZER, .cond = PTHREAD_COND_INITIALIZER};

static CGEventRef key_event(CGEventTapProxy proxy, CGEventType type, CGEventRef ev, void *ctx) {
    (void)proxy; (void)ctx;
    if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) {
        if (K.tap) CGEventTapEnable(K.tap, true);
        return ev;
    }
    if (type != kCGEventKeyDown || CGEventGetIntegerValueField(ev, kCGKeyboardEventAutorepeat))
        return ev;
    pthread_mutex_lock(&K.lock);
    if (K.count < KEYQ) {
        uint32_t at = (K.head + K.count++) % KEYQ;
        K.codes[at] = (uint32_t)CGEventGetIntegerValueField(ev, kCGKeyboardEventKeycode);
        K.flags[at] = CGEventGetFlags(ev);
        pthread_cond_signal(&K.cond);
    }
    pthread_mutex_unlock(&K.lock);
    return ev;
}

static void *key_thread(void *ctx) {
    (void)ctx;
    pthread_setname_np("Blurt hotkeys");
    CFMachPortRef tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                                         kCGEventTapOptionListenOnly, CGEventMaskBit(kCGEventKeyDown),
                                         key_event, NULL);
    pthread_mutex_lock(&K.lock);
    K.tap = tap;
    K.state = tap ? 1 : -1;
    pthread_cond_broadcast(&K.cond);
    pthread_mutex_unlock(&K.lock);
    if (!tap) return NULL;
    CFRunLoopSourceRef src = CFMachPortCreateRunLoopSource(NULL, tap, 0);
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes);
    CGEventTapEnable(tap, true);
    CFRunLoopRun();
    return NULL;
}

// 1 when keyboard monitoring is allowed. prompt=1 asks macOS to show the permission request.
EXPORT int ba_keys_access(int prompt) {
    if (CGPreflightListenEventAccess()) return 1;
    if (prompt) CGRequestListenEventAccess();
    return 0;
}

// Starts watching the keyboard; 1 on success, 0 without permission.
EXPORT int ba_keys_start(void) {
    pthread_mutex_lock(&K.lock);
    if (K.state == 1) { pthread_mutex_unlock(&K.lock); return 1; }
    K.state = 0;
    pthread_mutex_unlock(&K.lock);
    if (!CGPreflightListenEventAccess()) { K.state = -1; return 0; }
    pthread_t t;
    if (pthread_create(&t, NULL, key_thread, NULL) != 0) return 0;
    pthread_detach(t);
    pthread_mutex_lock(&K.lock);
    while (K.state == 0) pthread_cond_wait(&K.cond, &K.lock);
    int ok = K.state == 1;
    pthread_mutex_unlock(&K.lock);
    return ok;
}

// Waits up to timeout_s for a key press. Returns 1 and fills code (a macOS virtual key code)
// and flags (CGEventFlags), or 0 on timeout.
EXPORT int ba_keys_wait(uint32_t *code, uint64_t *flags, double timeout_s) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    double end = ts.tv_sec + ts.tv_nsec / 1e9 + timeout_s;
    ts.tv_sec = (time_t)end;
    ts.tv_nsec = (long)((end - (double)ts.tv_sec) * 1e9);
    pthread_mutex_lock(&K.lock);
    while (K.count == 0)
        if (pthread_cond_timedwait(&K.cond, &K.lock, &ts) != 0) break;
    int got = 0;
    if (K.count) {
        *code = K.codes[K.head];
        *flags = K.flags[K.head];
        K.head = (K.head + 1) % KEYQ;
        K.count--;
        got = 1;
    }
    pthread_mutex_unlock(&K.lock);
    return got;
}
