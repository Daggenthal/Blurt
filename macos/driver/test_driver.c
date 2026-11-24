/* Loads the built Blurt driver binary directly (no installation) and exercises it the way
 * coreaudiod would: properties, StartIO/StopIO, and the WriteMix -> ReadInput loopback.
 * Usage: test_driver path/to/Blurt.driver/Contents/MacOS/Blurt */
#include <CoreAudio/AudioServerPlugIn.h>
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MIX 2
#define MIX_STREAM 3
#define MIC 4
#define MIC_STREAM 5
#define RELAY 6
#define RELAY_STREAM 7
#define GAME 8
#define GAME_STREAM 9
#define CH 2
#define RING 16384

static int failures = 0;
#define CHECK(cond, ...)                                     \
    do {                                                     \
        if (cond) {                                          \
            printf("ok   ");                                 \
        } else {                                             \
            printf("FAIL ");                                 \
            failures++;                                      \
        }                                                    \
        printf(__VA_ARGS__);                                 \
        printf("\n");                                        \
    } while (0)

static int changes = 0;
static OSStatus HostPropertiesChanged(AudioServerPlugInHostRef h, AudioObjectID o, UInt32 n,
                                      const AudioObjectPropertyAddress *a) {
    (void)h; (void)o; (void)n; (void)a;
    changes++;
    return 0;
}
static OSStatus HostCopy(AudioServerPlugInHostRef h, CFStringRef k, CFPropertyListRef *d) {
    (void)h; (void)k;
    *d = NULL;
    return 0;
}
static OSStatus HostWrite(AudioServerPlugInHostRef h, CFStringRef k, CFPropertyListRef d) {
    (void)h; (void)k; (void)d;
    return 0;
}
static OSStatus HostDelete(AudioServerPlugInHostRef h, CFStringRef k) {
    (void)h; (void)k;
    return 0;
}
static OSStatus HostReqConfig(AudioServerPlugInHostRef h, AudioObjectID o, UInt64 a, void *i) {
    (void)h; (void)o; (void)a; (void)i;
    return 0;
}
static AudioServerPlugInHostInterface host = {
    HostPropertiesChanged, HostCopy, HostWrite, HostDelete, HostReqConfig,
};

static AudioServerPlugInDriverRef drv;

static AudioObjectPropertyAddress Addr(AudioObjectPropertySelector s, AudioObjectPropertyScope sc) {
    AudioObjectPropertyAddress a = {s, sc, kAudioObjectPropertyElementMain};
    return a;
}

static UInt32 GetU32(AudioObjectID o, AudioObjectPropertySelector s) {
    AudioObjectPropertyAddress a = Addr(s, kAudioObjectPropertyScopeGlobal);
    UInt32 v = 0xDEAD, size = 0;
    OSStatus e = (*drv)->GetPropertyData(drv, o, 0, &a, 0, NULL, sizeof(v), &size, &v);
    return e ? 0xFFFFFFFF : v;
}

static int StrIs(AudioObjectID o, AudioObjectPropertySelector s, const char *want) {
    AudioObjectPropertyAddress a = Addr(s, kAudioObjectPropertyScopeGlobal);
    CFStringRef str = NULL;
    UInt32 size = 0;
    if ((*drv)->GetPropertyData(drv, o, 0, &a, 0, NULL, sizeof(str), &size, &str) || !str)
        return 0;
    char buf[128];
    CFStringGetCString(str, buf, sizeof(buf), kCFStringEncodingUTF8);
    CFRelease(str);
    return strcmp(buf, want) == 0;
}

static void IO(AudioObjectID dev, AudioObjectID stream, UInt32 op, Float64 t, float *buf, UInt32 n) {
    AudioServerPlugInIOCycleInfo ci;
    memset(&ci, 0, sizeof(ci));
    ci.mInputTime.mSampleTime = t;
    ci.mOutputTime.mSampleTime = t;
    (*drv)->BeginIOOperation(drv, dev, 1, op, n, &ci);
    (*drv)->DoIOOperation(drv, dev, stream, 1, op, n, &ci, buf, NULL);
    (*drv)->EndIOOperation(drv, dev, 1, op, n, &ci);
}

static float Sine(Float64 t, int c) { return (float)(0.5 * sin(t * 0.01) + (c ? 0.001 : 0)); }

static float Relay(Float64 t, int c) { return (float)(0.25 * cos(t * 0.003) + (c ? 0.002 : 0)); }

static void WriteTo(AudioObjectID dev, float (*fn)(Float64, int), Float64 t, UInt32 n) {
    float *buf = malloc(n * CH * sizeof(float));
    for (UInt32 i = 0; i < n; i++)
        for (int c = 0; c < CH; c++) buf[i * CH + c] = fn(t + i, c);
    IO(dev, dev + 1, kAudioServerPlugInIOOperationWriteMix, t, buf, n);
    free(buf);
}
static void WriteSine(Float64 t, UInt32 n) { WriteTo(MIX, Sine, t, n); }

/* returns: 1 = all frames equal want(), 0 = all silent, -1 = anything else */
static int ReadFrom(AudioObjectID dev, float (*want)(Float64, int), Float64 t, UInt32 n) {
    float *buf = malloc(n * CH * sizeof(float));
    memset(buf, 0x7f, n * CH * sizeof(float)); /* garbage, must be overwritten */
    IO(dev, dev + 1, kAudioServerPlugInIOOperationReadInput, t, buf, n);
    int same = 1, silent = 1;
    for (UInt32 i = 0; i < n; i++)
        for (int c = 0; c < CH; c++) {
            if (fabsf(buf[i * CH + c] - want(t + i, c)) > 1e-6f) same = 0;
            if (buf[i * CH + c] != 0.0f) silent = 0;
        }
    free(buf);
    return same ? 1 : silent ? 0 : -1;
}
static int ReadKind(Float64 t, UInt32 n) { return ReadFrom(MIC, Sine, t, n); }
static float Both(Float64 t, int c) { return Sine(t, c) + Relay(t, c); }

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s path/to/Blurt\n", argv[0]);
        return 2;
    }
    void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    CHECK(lib != NULL, "dlopen %s", lib ? "" : dlerror());
    if (!lib) return 1;
    void *(*factory)(CFAllocatorRef, CFUUIDRef) = dlsym(lib, "BlurtDriver_Create");
    CHECK(factory != NULL, "factory symbol BlurtDriver_Create");
    if (!factory) return 1;
    CHECK(factory(NULL, kAudioServerPlugInDriverInterfaceUUID) == NULL, "factory rejects other types");
    drv = factory(NULL, kAudioServerPlugInTypeUUID);
    CHECK(drv != NULL, "factory returns driver for kAudioServerPlugInTypeUUID");

    void *iface = NULL;
    HRESULT hr = (*drv)->QueryInterface(drv, CFUUIDGetUUIDBytes(kAudioServerPlugInDriverInterfaceUUID), &iface);
    CHECK(hr == S_OK && iface == drv, "QueryInterface(driver interface)");
    hr = (*drv)->QueryInterface(drv, CFUUIDGetUUIDBytes(kAudioServerPlugInTypeUUID), &iface);
    CHECK(hr == E_NOINTERFACE, "QueryInterface rejects unknown interface");
    (*drv)->Release(drv);

    CHECK((*drv)->Initialize(drv, &host) == 0, "Initialize");

    /* plug-in: device list */
    AudioObjectPropertyAddress a = Addr(kAudioPlugInPropertyDeviceList, kAudioObjectPropertyScopeGlobal);
    UInt32 size = 0;
    AudioObjectID devs[6] = {0};
    (*drv)->GetPropertyDataSize(drv, 1, 0, &a, 0, NULL, &size);
    (*drv)->GetPropertyData(drv, 1, 0, &a, 0, NULL, sizeof(devs), &size, devs);
    CHECK(size == 4 * sizeof(AudioObjectID) && devs[0] == MIX && devs[1] == MIC &&
          devs[2] == RELAY && devs[3] == GAME,
          "device list = [%u, %u, %u, %u]", devs[0], devs[1], devs[2], devs[3]);

    const char *uids[4] = {"blurt_mix", "blurt_mic", "blurt_relay", "blurt_game_mic"};
    const AudioObjectID ids[4] = {MIX, MIC, RELAY, GAME};
    int translate_ok = 1;
    for (int i = 0; i < 4; i++) {
        CFStringRef uid = CFStringCreateWithCString(NULL, uids[i], kCFStringEncodingUTF8);
        AudioObjectID found = 0;
        a = Addr(kAudioPlugInPropertyTranslateUIDToDevice, kAudioObjectPropertyScopeGlobal);
        (*drv)->GetPropertyData(drv, 1, 0, &a, sizeof(uid), &uid, sizeof(found), &size, &found);
        CFRelease(uid);
        translate_ok &= found == ids[i] && StrIs(ids[i], kAudioDevicePropertyDeviceUID, uids[i]);
    }
    CHECK(translate_ok, "UIDs and TranslateUIDToDevice for all four devices");

    CHECK(StrIs(MIC, kAudioObjectPropertyName, "Blurt-Mix"), "Discord mic is named Blurt-Mix");
    CHECK(StrIs(GAME, kAudioObjectPropertyName, "Blurt-Friends"), "game mic is named Blurt-Friends");
    CHECK(StrIs(MIX, kAudioObjectPropertyName, "Blurt internal mix") &&
          StrIs(RELAY, kAudioObjectPropertyName, "Blurt internal relay"), "internal output names");
    CHECK(GetU32(MIX, kAudioDevicePropertyIsHidden) == 1 && GetU32(RELAY, kAudioDevicePropertyIsHidden) == 1 &&
          GetU32(MIC, kAudioDevicePropertyIsHidden) == 0 && GetU32(GAME, kAudioDevicePropertyIsHidden) == 0,
          "outputs hidden, mics visible");
    CHECK(GetU32(RELAY_STREAM, kAudioStreamPropertyDirection) == 0 &&
          GetU32(GAME_STREAM, kAudioStreamPropertyDirection) == 1, "relay/game stream directions");
    CHECK(StrIs(MIC, kAudioObjectPropertyManufacturer, "Blurt"), "manufacturer");
    CHECK(StrIs(MIC, kAudioDevicePropertyModelUID, "blurt_model"), "model UID");
    CHECK(GetU32(MIX, kAudioDevicePropertyDeviceCanBeDefaultDevice) == 0, "mix can't be default");
    CHECK(GetU32(MIX, kAudioDevicePropertyDeviceCanBeDefaultSystemDevice) == 0, "mix can't be system default");
    CHECK(GetU32(MIC, kAudioDevicePropertyDeviceCanBeDefaultDevice) == 1, "mic can be default");
    CHECK(GetU32(MIC, kAudioDevicePropertyDeviceCanBeDefaultSystemDevice) == 0, "mic can't be system default");
    CHECK(GetU32(MIX, kAudioDevicePropertyClockDomain) == GetU32(MIC, kAudioDevicePropertyClockDomain) &&
          GetU32(MIC, kAudioDevicePropertyClockDomain) != 0, "shared clock domain");
    CHECK(GetU32(MIX, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeVirtual, "virtual transport");
    CHECK(GetU32(MIX, kAudioDevicePropertyZeroTimeStampPeriod) == RING, "zero timestamp period");
    CHECK(GetU32(MIX_STREAM, kAudioStreamPropertyDirection) == 0 &&
          GetU32(MIC_STREAM, kAudioStreamPropertyDirection) == 1, "stream directions");

    /* streams per scope */
    a = Addr(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput);
    (*drv)->GetPropertyDataSize(drv, MIX, 0, &a, 0, NULL, &size);
    UInt32 mixIn = size;
    a.mScope = kAudioObjectPropertyScopeOutput;
    (*drv)->GetPropertyDataSize(drv, MIX, 0, &a, 0, NULL, &size);
    UInt32 mixOut = size;
    a.mScope = kAudioObjectPropertyScopeInput;
    (*drv)->GetPropertyDataSize(drv, MIC, 0, &a, 0, NULL, &size);
    UInt32 micIn = size;
    CHECK(mixIn == 0 && mixOut == 4 && micIn == 4, "streams by scope (mix in/out %u/%u, mic in %u)",
          mixIn, mixOut, micIn);

    /* format */
    AudioStreamBasicDescription f;
    a = Addr(kAudioStreamPropertyVirtualFormat, kAudioObjectPropertyScopeGlobal);
    (*drv)->GetPropertyData(drv, MIC_STREAM, 0, &a, 0, NULL, sizeof(f), &size, &f);
    CHECK(f.mSampleRate == 48000 && f.mChannelsPerFrame == 2 && f.mBitsPerChannel == 32 &&
          (f.mFormatFlags & kAudioFormatFlagIsFloat), "stream format 48 kHz stereo float32");
    Boolean settable = 0;
    (*drv)->IsPropertySettable(drv, MIC_STREAM, 0, &a, &settable);
    CHECK(settable, "virtual format settable");
    CHECK((*drv)->SetPropertyData(drv, MIC_STREAM, 0, &a, 0, NULL, sizeof(f), &f) == 0, "set same format ok");
    f.mSampleRate = 44100;
    CHECK((*drv)->SetPropertyData(drv, MIC_STREAM, 0, &a, 0, NULL, sizeof(f), &f) != 0, "set 44.1 kHz refused");
    a = Addr(kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal);
    Float64 rate = 44100;
    CHECK((*drv)->SetPropertyData(drv, MIX, 0, &a, 0, NULL, sizeof(rate), &rate) != 0, "nominal 44.1 kHz refused");
    a = Addr(kAudioDevicePropertyPreferredChannelLayout, kAudioObjectPropertyScopeOutput);
    (*drv)->GetPropertyDataSize(drv, MIX, 0, &a, 0, NULL, &size);
    CHECK(size > sizeof(AudioChannelLayout), "preferred channel layout size %u", size);
    a = Addr('zzzz', kAudioObjectPropertyScopeGlobal);
    CHECK(!(*drv)->HasProperty(drv, MIX, 0, &a), "unknown property not advertised");
    a = Addr(kAudioDevicePropertyLatency, kAudioObjectPropertyScopeOutput);
    CHECK((*drv)->HasProperty(drv, MIX, 0, &a), "latency advertised");

    Boolean will = 0, inPlace = 0;
    (*drv)->WillDoIOOperation(drv, MIX, 1, kAudioServerPlugInIOOperationWriteMix, &will, &inPlace);
    int ok1 = will;
    (*drv)->WillDoIOOperation(drv, MIX, 1, kAudioServerPlugInIOOperationReadInput, &will, &inPlace);
    int ok2 = !will;
    (*drv)->WillDoIOOperation(drv, MIC, 1, kAudioServerPlugInIOOperationReadInput, &will, &inPlace);
    int ok3 = will;
    CHECK(ok1 && ok2 && ok3, "WillDoIOOperation");

    /* clock */
    Float64 s1, s2;
    UInt64 h1, h2, seed;
    (*drv)->GetZeroTimeStamp(drv, MIX, 1, &s1, &h1, &seed);
    (*drv)->GetZeroTimeStamp(drv, MIC, 1, &s2, &h2, &seed);
    CHECK(s1 == s2 && h1 == h2 && seed == 1, "both devices share the zero time stamp");

    /* IO */
    changes = 0;
    CHECK((*drv)->StartIO(drv, MIX, 1) == 0 && (*drv)->StartIO(drv, MIC, 1) == 0, "StartIO both");
    CHECK(GetU32(MIX, kAudioDevicePropertyDeviceIsRunning) == 1, "mix is running");
    CHECK(changes == 2, "host notified of running changes (%d)", changes);

    Float64 t = RING * 6 + 16000; /* the 512-frame blocks cross the ring's wrap point */
    for (int b = 0; b < 4; b++) WriteSine(t + b * 512, 512);
    int good = 1;
    for (int b = 0; b < 4; b++) good &= ReadKind(t + b * 512, 512) == 1;
    CHECK(good, "loopback returns the sine across the ring wrap");
    CHECK(ReadKind(t, 512) == 1, "reading leaves the ring as it is (several mics read it)");
    CHECK(ReadFrom(GAME, Sine, t, 512) == 1, "Blurt-Friends carries the mix too");
    CHECK(ReadKind(t + 2048, 512) == 0, "reading beyond lastWriteEnd gives silence");

    /* partial: read straddling the write end */
    WriteSine(t + 4096, 256);
    {
        float buf[512 * CH];
        IO(MIC, MIC_STREAM, kAudioServerPlugInIOOperationReadInput, t + 4096, buf, 512);
        int firstHalf = 1, secondHalf = 1;
        for (int i = 0; i < 512; i++)
            for (int c = 0; c < CH; c++) {
                if (i < 256 && buf[i * CH + c] != Sine(t + 4096 + i, c)) firstHalf = 0;
                if (i >= 256 && buf[i * CH + c] != 0.0f) secondHalf = 0;
            }
        CHECK(firstHalf && secondHalf, "read straddling lastWriteEnd: data then silence");
    }

    /* stale data from a ring ago must not come back */
    WriteSine(t + 8192, 512);
    WriteSine(t + 8192 + 3 * RING, 512); /* big jump: old data is a ring+ older */
    CHECK(ReadKind(t + 8192, 512) == 0, "data older than one ring is silence");
    CHECK(ReadKind(t + 8192 + 3 * RING, 512) == 1, "newest data still reads back");

    /* gap: after a skipped stretch, the skipped frames read as silence, not old audio */
    Float64 g = t + 20 * RING;
    WriteSine(g, 512);
    WriteSine(g + RING / 2, 512); /* frames g+512 .. g+RING/2 skipped */
    CHECK(ReadKind(g + 1024, 512) == 0, "skipped frames read as silence");
    CHECK(ReadKind(g + RING / 2, 512) == 1, "data after the gap reads back");

    /* the relay: only Blurt-Friends carries it */
    CHECK((*drv)->StartIO(drv, RELAY, 1) == 0 && (*drv)->StartIO(drv, GAME, 1) == 0, "StartIO relay + game");
    Float64 r = g + 2 * RING;
    WriteSine(r, 512);
    WriteTo(RELAY, Relay, r, 512);
    CHECK(ReadFrom(GAME, Both, r, 512) == 1, "Blurt-Friends = mix + relay");
    CHECK(ReadFrom(MIC, Sine, r, 512) == 1, "Blurt-Mix = mix only (no relay: no echo in Discord)");
    WriteSine(r + 512, 512); /* relay stops writing: Blurt-Friends gets just the mix */
    CHECK(ReadFrom(GAME, Sine, r + 512, 512) == 1, "relay silent beyond its last write");

    /* a write with a wildly wrong (future) time must not make the mics go silent for good */
    WriteSine(r + 1024 + 1e12, 512);
    WriteSine(r + 1024, 512);
    WriteSine(r + 1536, 512);
    CHECK(ReadKind(r + 1536, 512) == 1, "recovers after a write far in the future");
    {   /* invalid times are ignored */
        float junk[512 * CH];
        for (int i = 0; i < 512 * CH; i++) junk[i] = 0.9f;
        IO(MIX, MIX_STREAM, kAudioServerPlugInIOOperationWriteMix, -1e9, junk, 512);
        IO(MIX, MIX_STREAM, kAudioServerPlugInIOOperationWriteMix, NAN, junk, 512);
    }
    CHECK(ReadKind(r + 1536, 512) == 1, "negative/NaN times ignored");
    CHECK((*drv)->StopIO(drv, RELAY, 1) == 0 && (*drv)->StopIO(drv, GAME, 1) == 0, "StopIO relay + game");

    /* stop the mix: mic hears silence */
    WriteSine(r + 4 * RING, 512);
    g = r + 3 * RING;
    CHECK((*drv)->StopIO(drv, MIX, 1) == 0, "StopIO mix");
    CHECK(GetU32(MIX, kAudioDevicePropertyDeviceIsRunning) == 0, "mix stopped");
    CHECK(ReadKind(g + RING, 512) == 0, "after StopIO of the mix, the mic reads silence");
    CHECK((*drv)->StopIO(drv, MIC, 1) == 0, "StopIO mic");
    CHECK((*drv)->StopIO(drv, MIC, 1) == 0 && GetU32(MIC, kAudioDevicePropertyDeviceIsRunning) == 0,
          "extra StopIO is harmless");

    printf("\n%s (%d failure%s)\n", failures ? "FAILED" : "ALL PASSED", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
