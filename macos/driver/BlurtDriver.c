/*
 * Blurt virtual audio driver (Core Audio AudioServerPlugIn).
 *
 * Publishes two devices that share one clock and one ring buffer:
 *   "Blurt-Mix" (UID blurt_mix)  output only: Blurt plays the mix (mic + sounds) into it
 *   "Blurt-Mic" (UID blurt_mic)  input only:  Discord, games, ... record from it
 *
 * Whatever is written to Blurt-Mix at sample time t is read back from Blurt-Mic at sample
 * time t. Both devices run at 48 kHz, 32-bit float, interleaved stereo.
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
};

#define kSampleRate 48000.0
#define kChannels 2
#define kRingFrames 16384 /* also the zero time stamp period */
#define kClockDomain 0x426C7274u /* 'Blrt' */
#define kBytesPerFrame (kChannels * (UInt32)sizeof(Float32))

#define kMixName "Blurt-Mix"
#define kMicName "Blurt-Mic"

/* ---- state ---------------------------------------------------------------------------- */
static AudioServerPlugInHostRef gHost = NULL;
static atomic_uint gRefCount = 1;
static os_unfair_lock gLock = OS_UNFAIR_LOCK_INIT;

static Float64 gTicksPerFrame = 0;
static UInt64 gAnchorHostTime = 0;
static UInt32 gIOCount[2] = {0, 0}; /* [0] mix, [1] mic */

static Float32 gRing[kRingFrames * kChannels];
static SInt64 gLastWriteEnd = 0;
static bool gHasWritten = false;

/* ---- small helpers -------------------------------------------------------------------- */
static bool IsDevice(AudioObjectID o) { return o == kObjMixDevice || o == kObjMicDevice; }
static bool IsStream(AudioObjectID o) { return o == kObjMixStream || o == kObjMicStream; }
static int DevIndex(AudioObjectID dev) { return dev == kObjMixDevice ? 0 : 1; }
static AudioObjectID StreamOf(AudioObjectID dev) {
    return dev == kObjMixDevice ? kObjMixStream : kObjMicStream;
}
static AudioObjectID DeviceOf(AudioObjectID stream) {
    return stream == kObjMixStream ? kObjMixDevice : kObjMicDevice;
}
/* does the device have a stream in this scope? (mix: output, mic: input) */
static bool DevHasScope(AudioObjectID dev, AudioObjectPropertyScope scope) {
    if (scope == kAudioObjectPropertyScopeGlobal) return true;
    if (dev == kObjMixDevice) return scope == kAudioObjectPropertyScopeOutput;
    return scope == kAudioObjectPropertyScopeInput;
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

static void RingZero(SInt64 t, UInt32 frames) {
    if (frames >= kRingFrames) {
        memset(gRing, 0, sizeof(gRing));
        return;
    }
    UInt32 i = RingIndex(t);
    UInt32 first = frames < kRingFrames - i ? frames : kRingFrames - i;
    memset(gRing + i * kChannels, 0, first * kBytesPerFrame);
    if (frames > first) memset(gRing, 0, (frames - first) * kBytesPerFrame);
}

static void RingWrite(SInt64 t, const Float32 *src, UInt32 frames) {
    UInt32 i = RingIndex(t);
    UInt32 first = frames < kRingFrames - i ? frames : kRingFrames - i;
    memcpy(gRing + i * kChannels, src, first * kBytesPerFrame);
    if (frames > first) memcpy(gRing, src + first * kChannels, (frames - first) * kBytesPerFrame);
}

/* copies out and zeroes what it read */
static void RingRead(SInt64 t, Float32 *dst, UInt32 frames) {
    UInt32 i = RingIndex(t);
    UInt32 first = frames < kRingFrames - i ? frames : kRingFrames - i;
    memcpy(dst, gRing + i * kChannels, first * kBytesPerFrame);
    memset(gRing + i * kChannels, 0, first * kBytesPerFrame);
    if (frames > first) {
        memcpy(dst + first * kChannels, gRing, (frames - first) * kBytesPerFrame);
        memset(gRing, 0, (frames - first) * kBytesPerFrame);
    }
}

static void RingReset(void) {
    memset(gRing, 0, sizeof(gRing));
    gLastWriteEnd = 0;
    gHasWritten = false;
}

static void MixWrite(SInt64 t, const Float32 *src, UInt32 frames) {
    os_unfair_lock_lock(&gLock);
    if (gHasWritten && t > gLastWriteEnd) {
        /* a gap (e.g. an overload): what is there is from one ring ago */
        SInt64 gap = t - gLastWriteEnd;
        RingZero(gLastWriteEnd, gap >= kRingFrames ? kRingFrames : (UInt32)gap);
    }
    if (frames > kRingFrames) { /* keep only the newest ring's worth */
        src += (frames - kRingFrames) * kChannels;
        t += frames - kRingFrames;
        frames = kRingFrames;
    }
    RingWrite(t, src, frames);
    SInt64 end = t + frames;
    if (!gHasWritten || end > gLastWriteEnd) gLastWriteEnd = end;
    gHasWritten = true;
    os_unfair_lock_unlock(&gLock);
}

static void MicRead(SInt64 t, Float32 *dst, UInt32 frames) {
    memset(dst, 0, (size_t)frames * kBytesPerFrame);
    os_unfair_lock_lock(&gLock);
    if (gHasWritten) {
        SInt64 lo = t, hi = t + frames;
        if (lo < gLastWriteEnd - kRingFrames) lo = gLastWriteEnd - kRingFrames;
        if (hi > gLastWriteEnd) hi = gLastWriteEnd;
        if (hi > lo) RingRead(lo, dst + (lo - t) * kChannels, (UInt32)(hi - lo));
    }
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
    static const AudioObjectID devices[2] = {kObjMixDevice, kObjMicDevice};
    switch (a->mSelector) {
    case kAudioObjectPropertyBaseClass: SCALAR(AudioClassID, kAudioObjectClassID);
    case kAudioObjectPropertyClass: SCALAR(AudioClassID, kAudioPlugInClassID);
    case kAudioObjectPropertyOwner: SCALAR(AudioObjectID, kAudioObjectUnknown);
    case kAudioObjectPropertyManufacturer: SCALAR(CFStringRef, CFSTR("Blurt"));
    case kAudioObjectPropertyOwnedObjects:
    case kAudioPlugInPropertyDeviceList:
        return Array(devices, 2, sizeof(AudioObjectID), inDataSize, outDataSize, outData);
    case kAudioObjectPropertyCustomPropertyInfoList:
        return Array(NULL, 0, sizeof(AudioServerPlugInCustomPropertyInfo), inDataSize,
                     outDataSize, outData);
    case kAudioPlugInPropertyTranslateUIDToDevice: {
        AudioObjectID found = kAudioObjectUnknown;
        if (outData) {
            if (qSize < sizeof(CFStringRef) || qData == NULL) return kAudioHardwareBadPropertySizeError;
            CFStringRef uid = *(const CFStringRef *)qData;
            if (uid && CFEqual(uid, CFSTR("blurt_mix"))) found = kObjMixDevice;
            else if (uid && CFEqual(uid, CFSTR("blurt_mic"))) found = kObjMicDevice;
        }
        SCALAR(AudioObjectID, found);
    }
    case kAudioPlugInPropertyResourceBundle: SCALAR(CFStringRef, CFSTR(""));
    default: return kAudioHardwareUnknownPropertyError;
    }
}

static OSStatus DeviceProp(AudioObjectID dev, const AudioObjectPropertyAddress *a,
                           UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    bool mix = dev == kObjMixDevice;
    switch (a->mSelector) {
    case kAudioObjectPropertyBaseClass: SCALAR(AudioClassID, kAudioObjectClassID);
    case kAudioObjectPropertyClass: SCALAR(AudioClassID, kAudioDeviceClassID);
    case kAudioObjectPropertyOwner: SCALAR(AudioObjectID, kObjPlugIn);
    case kAudioObjectPropertyName:
        SCALAR(CFStringRef, mix ? CFSTR(kMixName) : CFSTR(kMicName));
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
    case kAudioDevicePropertyDeviceUID:
        SCALAR(CFStringRef, mix ? CFSTR("blurt_mix") : CFSTR("blurt_mic"));
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
    case kAudioDevicePropertyDeviceCanBeDefaultDevice: SCALAR(UInt32, mix ? 0 : 1);
    case kAudioDevicePropertyDeviceCanBeDefaultSystemDevice: SCALAR(UInt32, 0);
    case kAudioDevicePropertyLatency: SCALAR(UInt32, 0);
    case kAudioDevicePropertySafetyOffset: SCALAR(UInt32, 0);
    case kAudioDevicePropertyNominalSampleRate: SCALAR(Float64, kSampleRate);
    case kAudioDevicePropertyAvailableNominalSampleRates: {
        AudioValueRange r = {kSampleRate, kSampleRate};
        return Array(&r, 1, sizeof(r), inDataSize, outDataSize, outData);
    }
    case kAudioDevicePropertyIsHidden: SCALAR(UInt32, 0);
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
    bool mix = stream == kObjMixStream;
    switch (a->mSelector) {
    case kAudioObjectPropertyBaseClass: SCALAR(AudioClassID, kAudioObjectClassID);
    case kAudioObjectPropertyClass: SCALAR(AudioClassID, kAudioStreamClassID);
    case kAudioObjectPropertyOwner: SCALAR(AudioObjectID, DeviceOf(stream));
    case kAudioObjectPropertyOwnedObjects:
        return Array(NULL, 0, sizeof(AudioObjectID), inDataSize, outDataSize, outData);
    case kAudioObjectPropertyName:
        SCALAR(CFStringRef, mix ? CFSTR(kMixName) : CFSTR(kMicName));
    case kAudioStreamPropertyIsActive: SCALAR(UInt32, 1);
    case kAudioStreamPropertyDirection: SCALAR(UInt32, mix ? 0 : 1); /* 1 = input */
    case kAudioStreamPropertyTerminalType:
        SCALAR(UInt32, mix ? kAudioStreamTerminalTypeLine : kAudioStreamTerminalTypeMicrophone);
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
    RingReset();
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
    if (first && dev == kObjMixDevice) RingReset();
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
        if (last && dev == kObjMixDevice) RingReset();
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
    bool yes = (dev == kObjMixDevice && op == kAudioServerPlugInIOOperationWriteMix) ||
               (dev == kObjMicDevice && op == kAudioServerPlugInIOOperationReadInput);
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
    if (op == kAudioServerPlugInIOOperationWriteMix && dev == kObjMixDevice &&
        stream == kObjMixStream) {
        MixWrite((SInt64)ci->mOutputTime.mSampleTime, (const Float32 *)main, frames);
    } else if (op == kAudioServerPlugInIOOperationReadInput && dev == kObjMicDevice &&
               stream == kObjMicStream) {
        MicRead((SInt64)ci->mInputTime.mSampleTime, (Float32 *)main, frames);
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
