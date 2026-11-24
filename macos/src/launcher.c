/*
 * Blurt.app's main executable. It runs Blurt on the Python embedded in the app
 * (Contents/Resources/python), in this same process, so macOS sees Blurt.app itself, which
 * matters for its permission prompts (microphone, system audio) and the Dock.
 *
 *   Blurt                 starts Blurt (Contents/Resources/app/blurt.py)
 *   Blurt -m module ...   behaves like `python -m module ...`; Blurt uses this to start its
 *   Blurt -c code ...     AI voice engine and to run pip, and libraries use it for helper
 *   Blurt -B -c code ...  processes (sys.executable points here). Any first argument that
 *                         starts with "-" means Python options.
 *
 * MIT License, part of Blurt (https://github.com/Daggenthal/Blurt).
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void fail(PyStatus status, PyConfig *config) {
    PyConfig_Clear(config);
    if (PyStatus_IsExit(status)) exit(status.exitcode);
    Py_ExitStatusException(status);
}

int main(int argc, char **argv) {
    char raw[PATH_MAX], exe[PATH_MAX], contents[PATH_MAX], home[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0 || realpath(raw, exe) == NULL) {
        fprintf(stderr, "Blurt: cannot find its own location\n");
        return 1;
    }
    char tmp[PATH_MAX];
    strlcpy(tmp, exe, sizeof tmp);
    strlcpy(contents, dirname(dirname(tmp)), sizeof contents);  /* .../Blurt.app/Contents */
    snprintf(home, sizeof home, "%s/Resources/python", contents);
    snprintf(script, sizeof script, "%s/Resources/app/blurt.py", contents);

    /* Started with Python options ("-m module", "-c code", or flags before them, such as
     * the "-B -c ..." that multiprocessing uses for its helper processes): act as the Python
     * interpreter. Blurt itself takes no options, so anything else starts Blurt. (Old macOS
     * versions add -psn_... when an app is started from Finder; that is dropped.) */
    int passthrough = argc > 1 && argv[1][0] == '-' && strncmp(argv[1], "-psn_", 5) != 0;
    char **args = calloc((size_t)argc + 2, sizeof(char *));
    int n = 0;
    args[n++] = exe;
    if (!passthrough) args[n++] = script;
    for (int i = 1; i < argc; i++)
        if (passthrough || strncmp(argv[i], "-psn_", 5) != 0) args[n++] = argv[i];

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.write_bytecode = 0;  /* never write .pyc files into the signed app */
    config.parse_argv = 1;
    PyStatus status;
    status = PyConfig_SetBytesString(&config, &config.home, home);
    if (PyStatus_Exception(status)) fail(status, &config);
    status = PyConfig_SetBytesString(&config, &config.program_name, exe);
    if (PyStatus_Exception(status)) fail(status, &config);
    status = PyConfig_SetBytesString(&config, &config.executable, exe);
    if (PyStatus_Exception(status)) fail(status, &config);
    status = PyConfig_SetBytesArgv(&config, n, args);
    if (PyStatus_Exception(status)) fail(status, &config);
    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) fail(status, &config);
    PyConfig_Clear(&config);
    return Py_RunMain();
}
