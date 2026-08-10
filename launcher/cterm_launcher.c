#ifdef __APPLE__

#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef CTERM_ENTRY_MODULE
#define CTERM_ENTRY_MODULE "cterm"
#endif

static int join_path(char *buffer, size_t size, const char *directory,
                     const char *suffix) {
    int written = snprintf(buffer, size, "%s/%s", directory, suffix);
    if (written < 0 || (size_t)written >= size) {
        fprintf(stderr, "cterm launcher: runtime path is too long\n");
        return -1;
    }
    return 0;
}

static int executable_path(char *buffer, size_t size) {
    uint32_t length = (uint32_t)size;
    char unresolved[PATH_MAX];
    if (_NSGetExecutablePath(unresolved, &length) != 0 ||
        realpath(unresolved, buffer) == NULL) {
        fprintf(stderr, "cterm launcher: cannot locate executable: %s\n",
                strerror(errno));
        return -1;
    }
    return 0;
}

static int bundled_python(const char *runtime, char *buffer, size_t size) {
    char bin_directory[PATH_MAX];
    if (join_path(bin_directory, sizeof(bin_directory), runtime, "bin") != 0) {
        return -1;
    }
    DIR *directory = opendir(bin_directory);
    if (directory == NULL) {
        fprintf(stderr, "cterm launcher: cannot open bundled bin directory: %s\n",
                strerror(errno));
        return -1;
    }

    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strncmp(entry->d_name, "python3.", 8) != 0 ||
            strstr(entry->d_name, "-config") != NULL) {
            continue;
        }
        char candidate[PATH_MAX];
        if (join_path(candidate, sizeof(candidate), bin_directory,
                      entry->d_name) != 0) {
            closedir(directory);
            return -1;
        }
        if (access(candidate, X_OK) == 0) {
            int written = snprintf(buffer, size, "%s", candidate);
            closedir(directory);
            if (written < 0 || (size_t)written >= size) {
                fprintf(stderr, "cterm launcher: interpreter path is too long\n");
                return -1;
            }
            return 0;
        }
    }
    closedir(directory);
    fprintf(stderr, "cterm launcher: bundled Python interpreter was not found\n");
    return -1;
}

int main(int argc, char **argv) {
    char executable[PATH_MAX];
    char runtime[PATH_MAX];
    char python[PATH_MAX];
    char python_path[PATH_MAX * 2];

    if (argc == 0 || executable_path(executable, sizeof(executable)) != 0) {
        return 1;
    }
    char *last_slash = strrchr(executable, '/');
    if (last_slash == NULL) {
        fprintf(stderr, "cterm launcher: executable has no parent directory\n");
        return 1;
    }
    *last_slash = '\0';
    if (snprintf(runtime, sizeof(runtime), "%s", executable) >=
        (int)sizeof(runtime)) {
        fprintf(stderr, "cterm launcher: runtime path is too long\n");
        return 1;
    }
    if (bundled_python(runtime, python, sizeof(python)) != 0) {
        return 1;
    }

    const char *existing = getenv("PYTHONPATH");
    int written = snprintf(
        python_path, sizeof(python_path), "%s/lib:%s%s%s", runtime, runtime,
        existing != NULL ? ":" : "", existing != NULL ? existing : "");
    if (written < 0 || (size_t)written >= sizeof(python_path)) {
        fprintf(stderr, "cterm launcher: PYTHONPATH is too long\n");
        return 1;
    }
    if (setenv("PYTHONPATH", python_path, 1) != 0) {
        fprintf(stderr, "cterm launcher: cannot set PYTHONPATH: %s\n",
                strerror(errno));
        return 1;
    }

    char *child_argv[argc + 4];
    child_argv[0] = python;
    child_argv[1] = "-m";
    child_argv[2] = CTERM_ENTRY_MODULE;
    for (int index = 1; index < argc; index++) {
        child_argv[index + 3] = argv[index];
    }
    child_argv[argc + 3] = NULL;
    execv(python, child_argv);
    fprintf(stderr, "cterm launcher: cannot execute Python: %s\n",
            strerror(errno));
    return 1;
}

#else

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#ifndef CTERM_ENTRY_MODULE
#define CTERM_ENTRY_MODULE "cterm.__main__"
#endif
#ifndef CTERM_ENTRY_FUNCTION
#define CTERM_ENTRY_FUNCTION "main"
#endif

#include <errno.h>
#include <dirent.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void print_status(const char *operation, PyStatus status) {
    if (PyStatus_IsExit(status)) {
        exit(status.exitcode);
    }
    if (PyStatus_IsError(status)) {
        fprintf(stderr, "cterm launcher: %s: %s\n", operation,
                status.err_msg ? status.err_msg : "Python initialization failed");
    }
}

static int executable_path(char *buffer, size_t size, const char *argv0) {
    ssize_t length = readlink("/proc/self/exe", buffer, size - 1);
    if (length >= 0) {
        buffer[length] = '\0';
        return 0;
    }

    if (realpath(argv0, buffer) != NULL) {
        return 0;
    }

    fprintf(stderr, "cterm launcher: cannot locate executable: %s\n", strerror(errno));
    return -1;
}

static int join_path(char *buffer, size_t size, const char *directory,
                     const char *suffix) {
    int written = snprintf(buffer, size, "%s/%s", directory, suffix);
    if (written < 0 || (size_t)written >= size) {
        fprintf(stderr, "cterm launcher: runtime path is too long\n");
        return -1;
    }
    return 0;
}

static int find_stdlib(const char *runtime, char *buffer, size_t size) {
    char lib_directory[PATH_MAX];
    if (join_path(lib_directory, sizeof(lib_directory), runtime, "lib") != 0) {
        return -1;
    }
    DIR *directory = opendir(lib_directory);
    if (directory == NULL) {
        fprintf(stderr, "cterm launcher: cannot open bundled lib directory: %s\n",
                strerror(errno));
        return -1;
    }

    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strncmp(entry->d_name, "python", 6) != 0) {
            continue;
        }
        char candidate[PATH_MAX];
        if (join_path(candidate, sizeof(candidate), lib_directory, entry->d_name) != 0) {
            closedir(directory);
            return -1;
        }
        struct stat details;
        if (stat(candidate, &details) == 0 && S_ISDIR(details.st_mode)) {
            int written = snprintf(buffer, size, "%s", candidate);
            closedir(directory);
            if (written < 0 || (size_t)written >= size) {
                fprintf(stderr, "cterm launcher: standard library path is too long\n");
                return -1;
            }
            return 0;
        }
    }
    closedir(directory);
    fprintf(stderr, "cterm launcher: bundled standard library was not found\n");
    return -1;
}

static int append_search_path(PyConfig *config, const char *path) {
    wchar_t *wide_path = Py_DecodeLocale(path, NULL);
    if (wide_path == NULL) {
        fprintf(stderr, "cterm launcher: cannot decode module search path\n");
        return -1;
    }
    PyStatus status = PyWideStringList_Append(&config->module_search_paths, wide_path);
    PyMem_RawFree(wide_path);
    if (PyStatus_Exception(status)) {
        print_status("append module search path", status);
        return -1;
    }
    return 0;
}

int main(int argc, char **argv) {
    char executable[PATH_MAX];
    char runtime[PATH_MAX];
    char application[PATH_MAX];
    char stdlib[PATH_MAX];
    char dynload[PATH_MAX];

    if (argc == 0 || executable_path(executable, sizeof(executable), argv[0]) != 0) {
        return 1;
    }
    char *last_slash = strrchr(executable, '/');
    if (last_slash == NULL) {
        fprintf(stderr, "cterm launcher: executable has no parent directory\n");
        return 1;
    }
    *last_slash = '\0';
    if (snprintf(runtime, sizeof(runtime), "%s", executable) >= (int)sizeof(runtime)) {
        fprintf(stderr, "cterm launcher: runtime path is too long\n");
        return 1;
    }
    if (find_stdlib(runtime, stdlib, sizeof(stdlib)) != 0 ||
        join_path(dynload, sizeof(dynload), stdlib, "lib-dynload") != 0 ||
        join_path(application, sizeof(application), runtime, "lib") != 0) {
        return 1;
    }

    PyConfig config;
    PyStatus status;
    PyConfig_InitIsolatedConfig(&config);
    config.parse_argv = 0;
    config.module_search_paths_set = 1;
    status = PyConfig_SetString(&config, &config.stdio_encoding, L"utf-8");
    if (PyStatus_Exception(status)) {
        print_status("set stdio encoding", status);
        PyConfig_Clear(&config);
        return 1;
    }
    status = PyConfig_SetString(&config, &config.stdio_errors, L"surrogateescape");
    if (PyStatus_Exception(status)) {
        print_status("set stdio errors", status);
        PyConfig_Clear(&config);
        return 1;
    }

    status = PyConfig_SetBytesString(&config, &config.program_name, executable);
    if (PyStatus_Exception(status)) {
        print_status("set program name", status);
        PyConfig_Clear(&config);
        return 1;
    }
    status = PyConfig_SetBytesString(&config, &config.home, runtime);
    if (PyStatus_Exception(status)) {
        print_status("set runtime home", status);
        PyConfig_Clear(&config);
        return 1;
    }
    status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (PyStatus_Exception(status)) {
        print_status("set process arguments", status);
        PyConfig_Clear(&config);
        return 1;
    }

    if (append_search_path(&config, runtime) != 0 ||
        append_search_path(&config, application) != 0 ||
        append_search_path(&config, stdlib) != 0 ||
        append_search_path(&config, dynload) != 0) {
        PyConfig_Clear(&config);
        return 1;
    }

    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        print_status("initialize Python", status);
        return 1;
    }

    PyObject *module = PyImport_ImportModule(CTERM_ENTRY_MODULE);
    if (module == NULL) {
        PyErr_Print();
        Py_FinalizeEx();
        return 1;
    }
    PyObject *main_function = PyObject_GetAttrString(module, CTERM_ENTRY_FUNCTION);
    Py_DECREF(module);
    if (main_function == NULL || !PyCallable_Check(main_function)) {
        PyErr_Print();
        Py_XDECREF(main_function);
        Py_FinalizeEx();
        return 1;
    }

    PyObject *result = PyObject_CallNoArgs(main_function);
    Py_DECREF(main_function);
    if (result == NULL) {
        PyErr_Print();
        Py_FinalizeEx();
        return 1;
    }
    int exit_code = 0;
    if (PyLong_Check(result)) {
        exit_code = (int)PyLong_AsLong(result);
    }
    Py_DECREF(result);
    if (Py_FinalizeEx() < 0) {
        return 120;
    }
    return exit_code;
}

#endif
