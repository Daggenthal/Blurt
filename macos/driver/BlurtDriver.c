/*
 * Blurt virtual audio driver (Core Audio AudioServerPlugIn).
 *
 * Publishes four devices that share one clock:
 *   "Blurt internal mix"   (UID blurt_mix,      output, hidden): Blurt plays the mix (your
 *                          mic + sounds) into it
 *   "Blurt internal relay" (UID blurt_relay,    output, hidden): Blurt plays the relayed app
 *                          (e.g. your Discord friends) into it
 *   "Blurt-Mix"            (UID blurt_mic,      input): the mix - the mic for Discord
 *   "Blurt-Friends"        (UID blurt_game_mic, input): the mix + the relay - for games
 * The outputs are hidden, so no app offers them; Blurt finds them by UID. The two mics have
 * names that say where each belongs: Discord must not get the relay, or your friends would
 * hear themselves.
 *
 * Whatever is written to an output at sample time t is read back from the mics at sample
 * time t. All devices run at 48 kHz, 32-bit float, interleaved stereo.
 *
 * MIT License, part of Blurt (https://github.com/Daggenthal/Blurt).
 */
#include <CoreAudio/AudioServerPlugIn.h>
#include <mach/mach_time.h>
#include <os/lock.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

/* ---- constants ------------------------------------------------------------------------ */
enum {
    kObjPlugIn = kAudioObjectPlugInObject, /* 1 */
    kObjMixDevice = 2,
    kObjMixStream = 3,
    kObjMicDevice = 4,
    kObjMicStream = 5,
    kObjRelayDevice = 6,
    kObjRelayStream = 7,
    kObjGameDevice = 8,
    kObjGameStream = 9,
};
#define kNumDevices 4

#define kSampleRate 48000.0
#define kChannels 2
#define kRingFrames 16384 /* also the zero time stamp period */
#define kClockDomain 0x426C7274u /* 'Blrt' */
#define kBytesPerFrame (kChannels * (UInt32)sizeof(Float32))

#define kMixName "Blurt internal mix"
#define kMicName "Blurt-Mix"
#define kRelayName "Blurt internal relay"
#define kGameName "Blurt-Friends"

/* ---- state ---------------------------------------------------------------------------- */
static AudioServerPlugInHostRef gHost = NULL;
static atomic_uint gRefCount = 1;
static os_unfair_lock gLock = OS_UNFAIR_LOCK_INIT;

static Float64 gTicksPerFrame = 0;
static UInt64 gAnchorHostTime = 0;
static UInt32 gIOCount[kNumDevices]; /* by DevIndex */

/* one ring per output: what was written at sample time t, for the mics to read at t */
typedef struct {
    Float32 data[kRingFrames * kChannels];
    SInt64 lastWriteEnd; /* end of the newest write */
    bool hasWritten;
} Ring;
static Ring gRings[2]; /* [0] Blurt-Mix, [1] Blurt-Relay */

/* ---- small helpers -------------------------------------------------------------------- */
static bool IsDevice(AudioObjectID o) {
    return o == kObjMixDevice || o == kObjMicDevice || o == kObjRelayDevice || o == kObjGameDevice;
}
static bool IsStream(AudioObjectID o) {
    return o == kObjMixStream || o == kObjMicStream || o == kObjRelayStream || o == kObjGameStream;
}
static int DevIndex(AudioObjectID dev) { return (int)(dev - kObjMixDevice) / 2; } /* 0..3 */
static AudioObjectID StreamOf(AudioObjectID dev) { return dev + 1; }
static AudioObjectID DeviceOf(AudioObjectID stream) { return stream - 1; }
static bool IsOutput(AudioObjectID dev) { return dev == kObjMixDevice || dev == kObjRelayDevice; }
/* the ring an output writes */
static Ring *RingOf(AudioObjectID dev) { return &gRings[dev == kObjRelayDevice ? 1 : 0]; }
/* does the device have a stream in this scope? (outputs: output, mics: input) */
static bool DevHasScope(AudioObjectID dev, AudioObjectPropertyScope scope) {
    if (scope == kAudioObjectPropertyScopeGlobal) return true;
    return scope == (IsOutput(dev) ? kAudioObjectPropertyScopeOutput : kAudioObjectPropertyScopeInput);
}
static CFStringRef DevName(AudioObjectID dev) {
    switch (dev) {
    case kObjMixDevice: return CFSTR(kMixName);
    case kObjRelayDevice: return CFSTR(kRelayName);
    case kObjGameDevice: return CFSTR(kGameName);
    default: return CFSTR(kMicName);
    }
}
static CFStringRef DevUID(AudioObjectID dev) {
    switch (dev) {
    case kObjMixDevice: return CFSTR("blurt_mix");
    case kObjRelayDevice: return CFSTR("blurt_relay");
    case kObjGameDevice: return CFSTR("blurt_game_mic");
    default: return CFSTR("blurt_mic");
    }
}

static void FillFormat(AudioStreamBasicDescription *f) {
    memset(f, 0, sizeof(*f));
    f->mSampleRate = kSampleRate;
    f->mFormatID = kAudioFormatLinearPCM;
    f->mFormatFlags = kAudioFormatFlagsNativeFloatPacked;
    f->mBytesPerPacket = kBytesPerFrame;
    f->mFramesPerPacket = 1;
    f->mBytesPerFrame = kBytesPerFrame;
    f->mChannelsPerFrame = kChannels;
    f->mBitsPerChannel = 32;
}

static bool FormatIsOurs(const AudioStreamBasicDescription *f) {
    return f->mSampleRate == kSampleRate && f->mFormatID == kAudioFormatLinearPCM &&
           f->mFormatFlags == kAudioFormatFlagsNativeFloatPacked &&
           f->mBytesPerFrame == kBytesPerFrame && f->mFramesPerPacket == 1 &&
           f->mChannelsPerFrame == kChannels && f->mBitsPerChannel == 32;
}

static void NotifyRunning(AudioObjectID dev) {
    if (gHost == NULL) return;
    /* (IsRunningSomewhere is derived by the HAL itself) */
    AudioObjectPropertyAddress a = {kAudioDevicePropertyDeviceIsRunning,
                                    kAudioObjectPropertyScopeGlobal,
                                    kAudioObjectPropertyElementMain};
    gHost->PropertiesChanged(gHost, dev, 1, &a);
}

/* ---- ring buffer (call with gLock held) ----------------------------------------------- */
static UInt32 RingIndex(SInt64 t) {
    SInt64 i = t % kRingFrames;
    return (UInt32)(i < 0 ? i + kRingFrames : i);
}

static void RingZero(Ring *r, SInt64 t, UInt32 frames) {
    if (frames >= kRingFrames) {
        memset(r->data, 0, sizeof(r->data));
        return;
    }
    UInt32 i = RingIndex(t);
    UInt32 first = frames < kRingFrames - i ? frames : kRingFrames - i;
    memset(r->data + i * kChannels, 0, first * kBytesPerFrame);
    if (frames > first) memset(r->data, 0, (frames - first) * kBytesPerFrame);
}

static void RingWrite(Ring *r, SInt64 t, const Float32 *src, UInt32 frames) {
    UInt32 i = RingIndex(t);
    UInt32 first = frames < kRingFrames - i ? frames : kRingFrames - i;
    memcpy(r->data + i * kChannels, src, first * kBytesPerFrame);
    if (frames > first) memcpy(r->data, src + first * kChannels, (frames - first) * kBytesPerFrame);
}

/* adds what was written for [t, t + frames) into dst (two mics can read the same ring, so
 * reading leaves it as it is) */
static void RingAdd(Ring *r, SInt64 t, Float32 *dst, UInt32 frames) {
    UInt32 i = RingIndex(t);
    UInt32 first = frames < kRingFrames - i ? frames : kRingFrames - i;
    const Float32 *a = r->data + i * kChannels;
    for (UInt32 k = 0; k < first * kChannels; k++) dst[k] += a[k];
    for (UInt32 k = 0; k < (frames - first) * kChannels; k++) dst[first * kChannels + k] += r->data[k];
}

static void RingReset(Ring *r) {
    memset(r->data, 0, sizeof(r->data));
    r->lastWriteEnd = 0;
    r->hasWritten = false;
}

static void OutputWrite(Ring *r, SInt64 t, const Float32 *src, UInt32 frames) {
    os_unfair_lock_lock(&gLock);
    if (r->hasWritten && t + frames <= r->lastWriteEnd - kRingFrames) {
        /* far behind the newest write: the timeline went back (or that write's time was
         * wrong). Start over from here; otherwise the mics would stay silent until the
         * timeline caught up with it again. */
        RingReset(r);
    }
    if (r->hasWritten && t > r->lastWriteEnd) {
        /* a gap (e.g. an overload): what is there is from one ring ago */
        SInt64 gap = t - r->lastWriteEnd;
        RingZero(r, r->lastWriteEnd, gap >= kRingFrames ? kRingFrames : (UInt32)gap);
    }
    if (frames > kRingFrames) { /* keep only the newest ring's worth */
        src += (frames - kRingFrames) * kChannels;
        t += frames - kRingFrames;
        frames = kRingFrames;
    }
    RingWrite(r, t, src, frames);
    SInt64 end = t + frames;
    if (!r->hasWritten || end > r->lastWriteEnd) r->lastWriteEnd = end;
    r->hasWritten = true;
    os_unfair_lock_unlock(&gLock);
}

/* adds the ring's audio for [t, t + frames) to dst, where it has any (call with gLock) */
static void RingMixInto(Ring *r, SInt64 t, Float32 *dst, UInt32 frames) {
    if (!r->hasWritten) return;
    SInt64 lo = t, hi = t + frames;
    if (lo < r->lastWriteEnd - kRingFrames) lo = r->lastWriteEnd - kRingFrames;
    if (hi > r->lastWriteEnd) hi = r->lastWriteEnd;
    if (hi > lo) RingAdd(r, lo, dst + (lo - t) * kChannels, (UInt32)(hi - lo));
}

/* Blurt-Mix (for Discord): the mix. Blurt-Friends (for games): the mix + the relay. */
static void MicRead(AudioObjectID dev, SInt64 t, Float32 *dst, UInt32 frames) {
    memset(dst, 0, (size_t)frames * kBytesPerFrame);
    os_unfair_lock_lock(&gLock);
    RingMixInto(&gRings[0], t, dst, frames);
    if (dev == kObjGameDevice) RingMixInto(&gRings[1], t, dst, frames);
    os_unfair_lock_unlock(&gLock);
}

/* ---- property access ------------------------------------------------------------------ */
/* One function answers size queries (outData == NULL) and reads. HasProperty is "a size
 * query does not fail with kAudioHardwareUnknownPropertyError". */

#define SCALAR(type, value)                                                       \
    do {                                                                          \
        if (outData) {                                                            \
            if (inDataSize < sizeof(type)) return kAudioHardwareBadPropertySizeError; \
            *(type *)outData = (value);                                           \
        }                                                                         \
        *outDataSize = (UInt32)sizeof(type);                                      \
        return kAudioHardwareNoError;                                             \
    } while (0)

static OSStatus Array(const void *items, UInt32 count, UInt32 itemSize, UInt32 inDataSize,
                      UInt32 *outDataSize, void *outData) {
    if (outData) {
        UInt32 n = inDataSize / itemSize;
        if (n > count) n = count;
        if (n) memcpy(outData, items, (size_t)n * itemSize);
        *outDataSize = n * itemSize;
    } else {
        *outDataSize = count * itemSize;
    }
    return kAudioHardwareNoError;
}

static OSStatus PlugInProp(const AudioObjectPropertyAddress *a, UInt32 qSize, const void *qData,
                           UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    static const AudioObjectID devices[kNumDevices] = {kObjMixDevice, kObjMicDevice,
                                                       kObjRelayDevice, kObjGameDevice};
    switch (a->mSelector) {
    case kAudioObjectPropertyBaseClass: SCALAR(AudioClassID, kAudioObjectClassID);
    case kAudioObjectPropertyClass: SCALAR(AudioClassID, kAudioPlugInClassID);
    case kAudioObjectPropertyOwner: SCALAR(AudioObjectID, kAudioObjectUnknown);
    case kAudioObjectPropertyManufacturer: SCALAR(CFStringRef, CFSTR("Blurt"));
    case kAudioObjectPropertyOwnedObjects:
    case kAudioPlugInPropertyDeviceList:
        return Array(devices, kNumDevices, sizeof(AudioObjectID), inDataSize, outDataSize, outData);
    case kAudioObjectPropertyCustomPropertyInfoList:
        return Array(NULL, 0, sizeof(AudioServerPlugInCustomPropertyInfo), inDataSize,
                     outDataSize, outData);
    case kAudioPlugInPropertyTranslateUIDToDevice: {
        AudioObjectID found = kAudioObjectUnknown;
        if (outData) {
            if (qSize < sizeof(CFStringRef) || qData == NULL) return kAudioHardwareBadPropertySizeError;
            CFStringRef uid = *(const CFStringRef *)qData;
            for (UInt32 i = 0; uid && i < kNumDevices; i++)
                if (CFEqual(uid, DevUID(devices[i]))) found = devices[i];
        }
        SCALAR(AudioObjectID, found);
    }
    case kAudioPlugInPropertyResourceBundle: SCALAR(CFStringRef, CFSTR(""));
    default: return kAudioHardwareUnknownPropertyError;
    }
}

static OSStatus DeviceProp(AudioObjectID dev, const AudioObjectPropertyAddress *a,
                           UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    bool output = IsOutput(dev);
    switch (a->mSelector) {
    case kAudioObjectPropertyBaseClass: SCALAR(AudioClassID, kAudioObjectClassID);
    case kAudioObjectPropertyClass: SCALAR(AudioClassID, kAudioDeviceClassID);
    case kAudioObjectPropertyOwner: SCALAR(AudioObjectID, kObjPlugIn);
    case kAudioObjectPropertyName: SCALAR(CFStringRef, DevName(dev));
    case kAudioObjectPropertyManufacturer: SCALAR(CFStringRef, CFSTR("Blurt"));
    case kAudioObjectPropertyOwnedObjects:
    case kAudioDevicePropertyStreams: {
        AudioObjectID s = StreamOf(dev);
        return Array(&s, DevHasScope(dev, a->mScope) ? 1 : 0, sizeof(AudioObjectID),
                     inDataSize, outDataSize, outData);
    }
    case kAudioObjectPropertyControlList:
        return Array(NULL, 0, sizeof(AudioObjectID), inDataSize, outDataSize, outData);
    case kAudioObjectPropertyCustomPropertyInfoList:
        return Array(NULL, 0, sizeof(AudioServerPlugInCustomPropertyInfo), inDataSize,
                     outDataSize, outData);
    case kAudioDevicePropertyDeviceUID: SCALAR(CFStringRef, DevUID(dev));
    case kAudioDevicePropertyModelUID: SCALAR(CFStringRef, CFSTR("blurt_model"));
    case kAudioDevicePropertyTransportType: SCALAR(UInt32, kAudioDeviceTransportTypeVirtual);
    case kAudioDevicePropertyRelatedDevices:
        return Array(&dev, 1, sizeof(AudioObjectID), inDataSize, outDataSize, outData);
    case kAudioDevicePropertyClockDomain: SCALAR(UInt32, kClockDomain);
    case kAudioDevicePropertyDeviceIsAlive: SCALAR(UInt32, 1);
    case kAudioDevicePropertyDeviceIsRunning: {
        UInt32 running = 0;
        if (outData) {
            os_unfair_lock_lock(&gLock);
            running = gIOCount[DevIndex(dev)] > 0;
            os_unfair_lock_unlock(&gLock);
        }
        SCALAR(UInt32, running);
    }
    case kAudioDevicePropertyDeviceCanBeDefaultDevice: SCALAR(UInt32, output ? 0 : 1);
    case kAudioDevicePropertyDeviceCanBeDefaultSystemDevice: SCALAR(UInt32, 0);
    case kAudioDevicePropertyLatency: SCALAR(UInt32, 0);
    case kAudioDevicePropertySafetyOffset: SCALAR(UInt32, 0);
    case kAudioDevicePropertyNominalSampleRate: SCALAR(Float64, kSampleRate);
    case kAudioDevicePropertyAvailableNominalSampleRates: {
        AudioValueRange r = {kSampleRate, kSampleRate};
        return Array(&r, 1, sizeof(r), inDataSize, outDataSize, outData);
    }
    case kAudioDevicePropertyIsHidden: SCALAR(UInt32, output ? 1 : 0);
    case kAudioDevicePropertyClockIsStable: SCALAR(UInt32, 1);
    case kAudioDevicePropertyZeroTimeStampPeriod: SCALAR(UInt32, kRingFrames);
    case kAudioDevicePropertyPreferredChannelsForStereo: {
        static const UInt32 ch[2] = {1, 2};
        return Array(ch, 2, sizeof(UInt32), inDataSize, outDataSize, outData);
    }
    case kAudioDevicePropertyPreferredChannelLayout: {
        UInt32 size = (UInt32)(offsetof(AudioChannelLayout, mChannelDescriptions) +
                               kChannels * sizeof(AudioChannelDescription));
        if (outData) {
            if (inDataSize < size) return kAudioHardwareBadPropertySizeError;
            AudioChannelLayout *l = (AudioChannelLayout *)outData;
            memset(l, 0, size);
            l->mChannelLayoutTag = kAudioChannelLayoutTag_UseChannelDescriptions;
            l->mNumberChannelDescriptions = kChannels;
            l->mChannelDescriptions[0].mChannelLabel = kAudioChannelLabel_Left;
            l->mChannelDescriptions[1].mChannelLabel = kAudioChannelLabel_Right;
        }
        *outDataSize = size;
        return kAudioHardwareNoError;
    }
    default: return kAudioHardwareUnknownPropertyError;
    }
}

static OSStatus StreamProp(AudioObjectID stream, const AudioObjectPropertyAddress *a,
                           UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    bool output = IsOutput(DeviceOf(stream));
    switch (a->mSelector) {
    case kAudioObjectPropertyBaseClass: SCALAR(AudioClassID, kAudioObjectClassID);
    case kAudioObjectPropertyClass: SCALAR(AudioClassID, kAudioStreamClassID);
    case kAudioObjectPropertyOwner: SCALAR(AudioObjectID, DeviceOf(stream));
    case kAudioObjectPropertyOwnedObjects:
        return Array(NULL, 0, sizeof(AudioObjectID), inDataSize, outDataSize, outData);
    case kAudioObjectPropertyName: SCALAR(CFStringRef, DevName(DeviceOf(stream)));
    case kAudioStreamPropertyIsActive: SCALAR(UInt32, 1);
    case kAudioStreamPropertyDirection: SCALAR(UInt32, output ? 0 : 1); /* 1 = input */
    case kAudioStreamPropertyTerminalType:
        SCALAR(UInt32, output ? kAudioStreamTerminalTypeLine : kAudioStreamTerminalTypeMicrophone);
    case kAudioStreamPropertyStartingChannel: SCALAR(UInt32, 1);
    case kAudioStreamPropertyLatency: SCALAR(UInt32, 0);
    case kAudioStreamPropertyVirtualFormat:
    case kAudioStreamPropertyPhysicalFormat: {
        AudioStreamBasicDescription f;
        FillFormat(&f);
        SCALAR(AudioStreamBasicDescription, f);
    }
    case kAudioStreamPropertyAvailableVirtualFormats:
    case kAudioStreamPropertyAvailablePhysicalFormats: {
        AudioStreamRangedDescription r;
        FillFormat(&r.mFormat);
        r.mSampleRateRange.mMinimum = r.mSampleRateRange.mMaximum = kSampleRate;
        return Array(&r, 1, sizeof(r), inDataSize, outDataSize, outData);
    }
    default: return kAudioHardwareUnknownPropertyError;
    }
}

static OSStatus Prop(AudioObjectID obj, const AudioObjectPropertyAddress *a, UInt32 qSize,
                     const void *qData, UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    if (a == NULL || outDataSize == NULL) return kAudioHardwareIllegalOperationError;
    if (obj == kObjPlugIn) return PlugInProp(a, qSize, qData, inDataSize, outDataSize, outData);
    if (IsDevice(obj)) return DeviceProp(obj, a, inDataSize, outDataSize, outData);
    if (IsStream(obj)) return StreamProp(obj, a, inDataSize, outDataSize, outData);
    return kAudioHardwareBadObjectError;
}

/* ---- driver interface ----------------------------------------------------------------- */
static HRESULT QueryInterface(void *drv, REFIID uuid, LPVOID *out);
static ULONG AddRef(void *drv);
static ULONG Release(void *drv);

static OSStatus Initialize(AudioServerPlugInDriverRef drv, AudioServerPlugInHostRef host) {
    (void)drv;
    gHost = host;
    mach_timebase_info_data_t tb;
    mach_timebase_info(&tb);
    Float64 ticksPerSecond = 1e9 * (Float64)tb.denom / (Float64)tb.numer;
    gTicksPerFrame = ticksPerSecond / kSampleRate;
    gAnchorHostTime = mach_absolute_time();
    os_unfair_lock_lock(&gLock);
    RingReset(&gRings[0]);
    RingReset(&gRings[1]);
    os_unfair_lock_unlock(&gLock);
    return kAudioHardwareNoError;
}

static OSStatus CreateDevice(AudioServerPlugInDriverRef d, CFDictionaryRef desc,
                             const AudioServerPlugInClientInfo *c, AudioObjectID *out) {
    (void)d; (void)desc; (void)c; (void)out;
    return kAudioHardwareUnsupportedOperationError;
}

static OSStatus DestroyDevice(AudioServerPlugInDriverRef d, AudioObjectID dev) {
    (void)d; (void)dev;
    return kAudioHardwareUnsupportedOperationError;
}

static OSStatus AddDeviceClient(AudioServerPlugInDriverRef d, AudioObjectID dev,
                                const AudioServerPlugInClientInfo *c) {
    (void)d; (void)c;
    return IsDevice(dev) ? kAudioHardwareNoError : kAudioHardwareBadObjectError;
}

static OSStatus RemoveDeviceClient(AudioServerPlugInDriverRef d, AudioObjectID dev,
                                   const AudioServerPlugInClientInfo *c) {
    (void)d; (void)c;
    return IsDevice(dev) ? kAudioHardwareNoError : kAudioHardwareBadObjectError;
}

static OSStatus PerformConfigChange(AudioServerPlugInDriverRef d, AudioObjectID dev,
                                    UInt64 action, void *info) {
    (void)d; (void)dev; (void)action; (void)info;
    return kAudioHardwareNoError;
}

static OSStatus AbortConfigChange(AudioServerPlugInDriverRef d, AudioObjectID dev,
                                  UInt64 action, void *info) {
    (void)d; (void)dev; (void)action; (void)info;
    return kAudioHardwareNoError;
}

static Boolean HasProperty(AudioServerPlugInDriverRef d, AudioObjectID obj, pid_t pid,
                           const AudioObjectPropertyAddress *a) {
    (void)d; (void)pid;
    UInt32 size = 0;
    return Prop(obj, a, 0, NULL, 0, &size, NULL) == kAudioHardwareNoError;
}

static OSStatus IsPropertySettable(AudioServerPlugInDriverRef d, AudioObjectID obj, pid_t pid,
                                   const AudioObjectPropertyAddress *a, Boolean *out) {
    (void)d; (void)pid;
    if (out == NULL) return kAudioHardwareIllegalOperationError;
    UInt32 size = 0;
    OSStatus err = Prop(obj, a, 0, NULL, 0, &size, NULL);
    if (err) return err;
    *out = (IsDevice(obj) && a->mSelector == kAudioDevicePropertyNominalSampleRate) ||
           (IsStream(obj) && (a->mSelector == kAudioStreamPropertyVirtualFormat ||
                              a->mSelector == kAudioStreamPropertyPhysicalFormat));
    return kAudioHardwareNoError;
}

static OSStatus GetPropertyDataSize(AudioServerPlugInDriverRef d, AudioObjectID obj, pid_t pid,
                                    const AudioObjectPropertyAddress *a, UInt32 qSize,
                                    const void *qData, UInt32 *outSize) {
    (void)d; (void)pid;
    return Prop(obj, a, qSize, qData, 0, outSize, NULL);
}

static OSStatus GetPropertyData(AudioServerPlugInDriverRef d, AudioObjectID obj, pid_t pid,
                                const AudioObjectPropertyAddress *a, UInt32 qSize,
                                const void *qData, UInt32 inSize, UInt32 *outSize, void *out) {
    (void)d; (void)pid;
    if (out == NULL) return kAudioHardwareIllegalOperationError;
    return Prop(obj, a, qSize, qData, inSize, outSize, out);
}

static OSStatus SetPropertyData(AudioServerPlugInDriverRef d, AudioObjectID obj, pid_t pid,
                                const AudioObjectPropertyAddress *a, UInt32 qSize,
                                const void *qData, UInt32 inSize, const void *data) {
    (void)d; (void)pid; (void)qSize; (void)qData;
    if (a == NULL || data == NULL) return kAudioHardwareIllegalOperationError;
    if (IsDevice(obj) && a->mSelector == kAudioDevicePropertyNominalSampleRate) {
        if (inSize < sizeof(Float64)) return kAudioHardwareBadPropertySizeError;
        return *(const Float64 *)data == kSampleRate ? kAudioHardwareNoError
                                                     : kAudioDeviceUnsupportedFormatError;
    }
    if (IsStream(obj) && (a->mSelector == kAudioStreamPropertyVirtualFormat ||
                          a->mSelector == kAudioStreamPropertyPhysicalFormat)) {
        if (inSize < sizeof(AudioStreamBasicDescription)) return kAudioHardwareBadPropertySizeError;
        return FormatIsOurs((const AudioStreamBasicDescription *)data)
                   ? kAudioHardwareNoError : kAudioDeviceUnsupportedFormatError;
    }
    UInt32 size = 0;
    if (Prop(obj, a, 0, NULL, 0, &size, NULL) != kAudioHardwareNoError)
        return IsDevice(obj) || IsStream(obj) || obj == kObjPlugIn
                   ? kAudioHardwareUnknownPropertyError : kAudioHardwareBadObjectError;
    return kAudioHardwareUnsupportedOperationError;
}

static OSStatus StartIO(AudioServerPlugInDriverRef d, AudioObjectID dev, UInt32 client) {
    (void)d; (void)client;
    if (!IsDevice(dev)) return kAudioHardwareBadObjectError;
    os_unfair_lock_lock(&gLock);
    bool first = gIOCount[DevIndex(dev)]++ == 0;
    if (first && IsOutput(dev)) RingReset(RingOf(dev));
    os_unfair_lock_unlock(&gLock);
    if (first) NotifyRunning(dev);
    return kAudioHardwareNoError;
}

static OSStatus StopIO(AudioServerPlugInDriverRef d, AudioObjectID dev, UInt32 client) {
    (void)d; (void)client;
    if (!IsDevice(dev)) return kAudioHardwareBadObjectError;
    bool last = false;
    os_unfair_lock_lock(&gLock);
    UInt32 *n = &gIOCount[DevIndex(dev)];
    if (*n > 0) {
        last = --*n == 0;
        if (last && IsOutput(dev)) RingReset(RingOf(dev));
    }
    os_unfair_lock_unlock(&gLock);
    if (last) NotifyRunning(dev);
    return kAudioHardwareNoError;
}

static OSStatus GetZeroTimeStamp(AudioServerPlugInDriverRef d, AudioObjectID dev, UInt32 client,
                                 Float64 *outSampleTime, UInt64 *outHostTime, UInt64 *outSeed) {
    (void)d; (void)client;
    if (!IsDevice(dev)) return kAudioHardwareBadObjectError;
    /* stateless and shared: both devices always agree on the sample time */
    Float64 ticksPerPeriod = gTicksPerFrame * kRingFrames;
    UInt64 now = mach_absolute_time();
    Float64 periods = now > gAnchorHostTime
                          ? (Float64)(UInt64)((Float64)(now - gAnchorHostTime) / ticksPerPeriod)
                          : 0.0;
    *outSampleTime = periods * kRingFrames;
    *outHostTime = gAnchorHostTime + (UInt64)(periods * ticksPerPeriod);
    *outSeed = 1;
    return kAudioHardwareNoError;
}

static OSStatus WillDoIOOperation(AudioServerPlugInDriverRef d, AudioObjectID dev, UInt32 client,
                                  UInt32 op, Boolean *outWillDo, Boolean *outInPlace) {
    (void)d; (void)client;
    if (!IsDevice(dev)) return kAudioHardwareBadObjectError;
    bool yes = IsOutput(dev) ? op == kAudioServerPlugInIOOperationWriteMix
                             : op == kAudioServerPlugInIOOperationReadInput;
    if (outWillDo) *outWillDo = yes;
    if (outInPlace) *outInPlace = true;
    return kAudioHardwareNoError;
}

static OSStatus BeginIOOperation(AudioServerPlugInDriverRef d, AudioObjectID dev, UInt32 client,
                                 UInt32 op, UInt32 frames, const AudioServerPlugInIOCycleInfo *ci) {
    (void)d; (void)client; (void)op; (void)frames; (void)ci;
    return IsDevice(dev) ? kAudioHardwareNoError : kAudioHardwareBadObjectError;
}

static OSStatus DoIOOperation(AudioServerPlugInDriverRef d, AudioObjectID dev, AudioObjectID stream,
                              UInt32 client, UInt32 op, UInt32 frames,
                              const AudioServerPlugInIOCycleInfo *ci, void *main, void *secondary) {
    (void)d; (void)client; (void)secondary;
    if (!IsDevice(dev)) return kAudioHardwareBadObjectError;
    if (ci == NULL || main == NULL) return kAudioHardwareIllegalOperationError;
    if (stream != StreamOf(dev)) return kAudioHardwareNoError;
    if (op == kAudioServerPlugInIOOperationWriteMix && IsOutput(dev)) {
        Float64 t = ci->mOutputTime.mSampleTime;
        if (t >= 0 && t < 9e15) OutputWrite(RingOf(dev), (SInt64)t, (const Float32 *)main, frames);
    } else if (op == kAudioServerPlugInIOOperationReadInput && !IsOutput(dev)) {
        Float64 t = ci->mInputTime.mSampleTime;
        if (t >= 0 && t < 9e15) MicRead(dev, (SInt64)t, (Float32 *)main, frames);
        else memset(main, 0, (size_t)frames * kBytesPerFrame);
    }
    return kAudioHardwareNoError;
}

static OSStatus EndIOOperation(AudioServerPlugInDriverRef d, AudioObjectID dev, UInt32 client,
                               UInt32 op, UInt32 frames, const AudioServerPlugInIOCycleInfo *ci) {
    (void)d; (void)client; (void)op; (void)frames; (void)ci;
    return IsDevice(dev) ? kAudioHardwareNoError : kAudioHardwareBadObjectError;
}

static AudioServerPlugInDriverInterface gInterface = {
    ._reserved = NULL,
    .QueryInterface = QueryInterface,
    .AddRef = AddRef,
    .Release = Release,
    .Initialize = Initialize,
    .CreateDevice = CreateDevice,
    .DestroyDevice = DestroyDevice,
    .AddDeviceClient = AddDeviceClient,
    .RemoveDeviceClient = RemoveDeviceClient,
    .PerformDeviceConfigurationChange = PerformConfigChange,
    .AbortDeviceConfigurationChange = AbortConfigChange,
    .HasProperty = HasProperty,
    .IsPropertySettable = IsPropertySettable,
    .GetPropertyDataSize = GetPropertyDataSize,
    .GetPropertyData = GetPropertyData,
    .SetPropertyData = SetPropertyData,
    .StartIO = StartIO,
    .StopIO = StopIO,
    .GetZeroTimeStamp = GetZeroTimeStamp,
    .WillDoIOOperation = WillDoIOOperation,
    .BeginIOOperation = BeginIOOperation,
    .DoIOOperation = DoIOOperation,
    .EndIOOperation = EndIOOperation,
};
static AudioServerPlugInDriverInterface *gInterfacePtr = &gInterface;
static AudioServerPlugInDriverRef gDriverRef = &gInterfacePtr;

static HRESULT QueryInterface(void *drv, REFIID uuid, LPVOID *out) {
    if (drv != gDriverRef || out == NULL) return kAudioHardwareBadObjectError;
    CFUUIDRef asked = CFUUIDCreateFromUUIDBytes(NULL, uuid);
    if (asked == NULL) return E_NOINTERFACE;
    bool ok = CFEqual(asked, IUnknownUUID) || CFEqual(asked, kAudioServerPlugInDriverInterfaceUUID);
    CFRelease(asked);
    if (!ok) {
        *out = NULL;
        return E_NOINTERFACE;
    }
    atomic_fetch_add(&gRefCount, 1);
    *out = gDriverRef;
    return S_OK;
}

static ULONG AddRef(void *drv) {
    if (drv != gDriverRef) return 0;
    return atomic_fetch_add(&gRefCount, 1) + 1;
}

static ULONG Release(void *drv) {
    if (drv != gDriverRef) return 0;
    unsigned cur = atomic_load(&gRefCount);
    while (cur > 0 && !atomic_compare_exchange_weak(&gRefCount, &cur, cur - 1)) {
    }
    return cur > 0 ? cur - 1 : 0; /* static object: nothing to free */
}

/* The factory named in Info.plist (CFPlugInFactories). */
__attribute__((visibility("default"))) void *BlurtDriver_Create(CFAllocatorRef alloc,
                                                                CFUUIDRef requestedType) {
    (void)alloc;
    if (requestedType && CFEqual(requestedType, kAudioServerPlugInTypeUUID)) return gDriverRef;
    return NULL;
}
