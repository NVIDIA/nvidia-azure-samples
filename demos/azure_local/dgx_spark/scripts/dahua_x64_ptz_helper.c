// x86_64 helper for Dahua/Amcrest PTZ control via NetSDK.
//
// ARM64 Python launches this under qemu-x86_64-static when only an x86_64
// libdhnetsdk.so is available locally.

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

typedef int BOOL;
typedef uint32_t DWORD;
typedef int64_t LLONG;
typedef unsigned char BYTE;
typedef unsigned long LDWORD;

enum {
    EM_LOGIN_SPEC_CAP_TCP = 0,
    DH_PTZ_UP_CONTROL = 0,
    DH_PTZ_DOWN_CONTROL = 1,
    DH_PTZ_LEFT_CONTROL = 2,
    DH_PTZ_RIGHT_CONTROL = 3,
    DH_PTZ_ZOOM_ADD_CONTROL = 10,
    DH_PTZ_ZOOM_DEC_CONTROL = 11,
    DH_EXTPTZ_LEFTTOP = 0x20,
    DH_EXTPTZ_LEFTDOWN = 0x22,
};

typedef struct {
    BYTE sSerialNumber[48];
    int nAlarmInPortNum;
    int nAlarmOutPortNum;
    int nDiskNum;
    int nDVRType;
    int nChanNum;
    BYTE byLimitLoginTime;
    BYTE byLeftLogTimes;
    BYTE bReserved[2];
    int nLockLeftTime;
    char Reserved[24];
} NET_DEVICEINFO_Ex;

typedef void (*fDisConnect)(LLONG, char *, long, LDWORD);

typedef BOOL (*CLIENT_Init_t)(fDisConnect, LDWORD);
typedef void (*CLIENT_Cleanup_t)(void);
typedef DWORD (*CLIENT_GetLastError_t)(void);
typedef void (*CLIENT_SetConnectTime_t)(int, int);
typedef LLONG (*CLIENT_LoginEx2_t)(const char *, uint16_t, const char *, const char *, int, void *, NET_DEVICEINFO_Ex *, int *);
typedef BOOL (*CLIENT_Logout_t)(LLONG);
typedef BOOL (*CLIENT_DHPTZControlEx_t)(LLONG, int, DWORD, int, int, int, BOOL);
typedef BOOL (*CLIENT_DHPTZControlEx2_t)(LLONG, int, DWORD, int, int, int, BOOL, void *);
typedef BOOL (*CLIENT_PTZControl_t)(LLONG, int, DWORD, DWORD, DWORD);

typedef struct {
    CLIENT_Init_t CLIENT_Init;
    CLIENT_Cleanup_t CLIENT_Cleanup;
    CLIENT_GetLastError_t CLIENT_GetLastError;
    CLIENT_SetConnectTime_t CLIENT_SetConnectTime;
    CLIENT_LoginEx2_t CLIENT_LoginEx2;
    CLIENT_Logout_t CLIENT_Logout;
    CLIENT_DHPTZControlEx_t CLIENT_DHPTZControlEx;
    CLIENT_DHPTZControlEx2_t CLIENT_DHPTZControlEx2;
    CLIENT_PTZControl_t CLIENT_PTZControl;
} SDK;

typedef struct {
    const char *sdk_lib;
    const char *host;
    const char *user;
    const char *password;
    const char *password_env;
    const char *command;
    const char *action;
    int port;
    int channel;
    int speed;
    int timeout_seconds;
    int duration_ms;
    int stdin_server;
    int raw_ptz_code;
    int raw_param1;
    int raw_param2;
} Options;

typedef struct {
    const char *name;
    DWORD code;
    int use_param1;
    int use_param2;
} PTZCommand;

typedef struct {
    const PTZCommand *ptz;
    int pulse;
    int moving;
    int param1;
    int param2;
    BOOL stop;
    const char *control_api;
    BOOL pulse_stop_ok;
    DWORD pulse_stop_error;
} PTZResult;

static void disconnect_cb(LLONG login, char *ip, long port, LDWORD user) {
    (void)login;
    (void)ip;
    (void)port;
    (void)user;
}

static void json_escape(const char *s) {
    if (!s) {
        return;
    }
    for (; *s; ++s) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            putchar('\\');
            putchar(c);
        } else if (c == '\n') {
            fputs("\\n", stdout);
        } else if (c == '\r') {
            fputs("\\r", stdout);
        } else if (c == '\t') {
            fputs("\\t", stdout);
        } else if (c < 32) {
            printf("\\u%04x", c);
        } else {
            putchar(c);
        }
    }
}

static int emit_error(const char *message, const Options *opt, int native_port_open, const char *sdk_lib) {
    printf("{\"status\":\"error\",\"backend\":\"x64_netsdk_qemu\",\"error\":\"");
    json_escape(message);
    printf("\",\"native_port_open\":%s", native_port_open ? "true" : "false");
    if (sdk_lib && *sdk_lib) {
        printf(",\"sdk_lib\":\"");
        json_escape(sdk_lib);
        printf("\"");
    }
    if (opt && opt->host) {
        printf(",\"host\":\"");
        json_escape(opt->host);
        printf("\",\"port\":%d", opt->port);
    }
    if (opt && opt->command) {
        printf(",\"command\":\"");
        json_escape(opt->command);
        printf("\"");
    }
    if (opt && opt->action) {
        printf(",\"action\":\"");
        json_escape(opt->action);
        printf("\"");
    }
    printf("}\n");
    return 1;
}

static int tcp_port_open(const char *host, int port, int timeout_seconds) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return 0;
    }
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        close(fd);
        return 0;
    }
    int rc = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
    if (rc == 0) {
        close(fd);
        return 1;
    }
    if (errno != EINPROGRESS) {
        close(fd);
        return 0;
    }
    fd_set wfds;
    FD_ZERO(&wfds);
    FD_SET(fd, &wfds);
    struct timeval tv;
    tv.tv_sec = timeout_seconds > 0 ? timeout_seconds : 1;
    tv.tv_usec = 0;
    rc = select(fd + 1, NULL, &wfds, NULL, &tv);
    if (rc <= 0) {
        close(fd);
        return 0;
    }
    int err = 0;
    socklen_t len = sizeof(err);
    getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len);
    close(fd);
    return err == 0;
}

static int load_symbol(void *handle, const char *name, void **target) {
    dlerror();
    *target = dlsym(handle, name);
    const char *err = dlerror();
    if (err || !*target) {
        return -1;
    }
    return 0;
}

static int load_sdk(const char *path, void **handle_out, SDK *sdk, char *errbuf, size_t errbuf_size) {
    void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        snprintf(errbuf, errbuf_size, "dlopen failed: %s", dlerror());
        return -1;
    }
#define REQ(name) \
    if (load_symbol(handle, #name, (void **)&sdk->name) != 0) { \
        snprintf(errbuf, errbuf_size, "missing symbol %s", #name); \
        dlclose(handle); \
        return -1; \
    }
    REQ(CLIENT_Init)
    REQ(CLIENT_Cleanup)
    REQ(CLIENT_GetLastError)
    REQ(CLIENT_LoginEx2)
    REQ(CLIENT_Logout)
    REQ(CLIENT_DHPTZControlEx)
#undef REQ
    sdk->CLIENT_SetConnectTime = (CLIENT_SetConnectTime_t)dlsym(handle, "CLIENT_SetConnectTime");
    sdk->CLIENT_DHPTZControlEx2 = (CLIENT_DHPTZControlEx2_t)dlsym(handle, "CLIENT_DHPTZControlEx2");
    sdk->CLIENT_PTZControl = (CLIENT_PTZControl_t)dlsym(handle, "CLIENT_PTZControl");
    *handle_out = handle;
    return 0;
}

static void usage(const char *prog) {
    fprintf(stderr, "Usage: %s --sdk-lib PATH --host IP --port 37777 --user USER (--password PASSWORD | --password-env ENV) [--stdin-server | --command up|down|left|right|zoom_in|zoom_out --action start|stop|pulse] [--channel 0] [--speed 1] [--duration-ms 120] [--ptz-code N --param1 N --param2 N]\n", prog);
}

static int parse_int(const char *value, int fallback) {
    if (!value) {
        return fallback;
    }
    char *end = NULL;
    long parsed = strtol(value, &end, 10);
    if (!end || *end != '\0') {
        return fallback;
    }
    return (int)parsed;
}

static int parse_options(int argc, char **argv, Options *opt) {
    memset(opt, 0, sizeof(*opt));
    opt->port = 37777;
    opt->channel = 0;
    opt->speed = 1;
    opt->timeout_seconds = 5;
    opt->duration_ms = 120;
    opt->action = "start";
    opt->raw_ptz_code = -1;
    opt->raw_param1 = 0;
    opt->raw_param2 = 0;
    for (int i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        const char *value = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (strcmp(arg, "--sdk-lib") == 0 && value) {
            opt->sdk_lib = value;
            ++i;
        } else if (strcmp(arg, "--host") == 0 && value) {
            opt->host = value;
            ++i;
        } else if (strcmp(arg, "--port") == 0 && value) {
            opt->port = parse_int(value, opt->port);
            ++i;
        } else if (strcmp(arg, "--user") == 0 && value) {
            opt->user = value;
            ++i;
        } else if (strcmp(arg, "--password") == 0 && value) {
            opt->password = value;
            ++i;
        } else if (strcmp(arg, "--password-env") == 0 && value) {
            opt->password_env = value;
            ++i;
        } else if (strcmp(arg, "--command") == 0 && value) {
            opt->command = value;
            ++i;
        } else if (strcmp(arg, "--action") == 0 && value) {
            opt->action = value;
            ++i;
        } else if (strcmp(arg, "--channel") == 0 && value) {
            opt->channel = parse_int(value, opt->channel);
            ++i;
        } else if (strcmp(arg, "--speed") == 0 && value) {
            opt->speed = parse_int(value, opt->speed);
            ++i;
        } else if (strcmp(arg, "--timeout") == 0 && value) {
            opt->timeout_seconds = parse_int(value, opt->timeout_seconds);
            ++i;
        } else if (strcmp(arg, "--duration-ms") == 0 && value) {
            opt->duration_ms = parse_int(value, opt->duration_ms);
            ++i;
        } else if (strcmp(arg, "--ptz-code") == 0 && value) {
            opt->raw_ptz_code = parse_int(value, opt->raw_ptz_code);
            ++i;
        } else if (strcmp(arg, "--param1") == 0 && value) {
            opt->raw_param1 = parse_int(value, opt->raw_param1);
            ++i;
        } else if (strcmp(arg, "--param2") == 0 && value) {
            opt->raw_param2 = parse_int(value, opt->raw_param2);
            ++i;
        } else if (strcmp(arg, "--stdin-server") == 0) {
            opt->stdin_server = 1;
        } else {
            usage(argv[0]);
            return -1;
        }
    }
    if (!opt->password && opt->password_env) {
        opt->password = getenv(opt->password_env);
    }
    if (!opt->command && opt->raw_ptz_code >= 0) {
        opt->command = "raw";
    }
    if (!opt->sdk_lib || !opt->host || !opt->user || !opt->password || !*opt->password || (!opt->stdin_server && (!opt->command || !opt->action))) {
        usage(argv[0]);
        return -1;
    }
    if (!opt->stdin_server && strcmp(opt->action, "start") != 0 && strcmp(opt->action, "stop") != 0 && strcmp(opt->action, "pulse") != 0) {
        usage(argv[0]);
        return -1;
    }
    if (opt->speed < 1) {
        opt->speed = 1;
    } else if (opt->speed > 8) {
        opt->speed = 8;
    }
    if (opt->duration_ms < 30) {
        opt->duration_ms = 30;
    } else if (opt->duration_ms > 1000) {
        opt->duration_ms = 1000;
    }
    return 0;
}

static const PTZCommand *find_ptz_command(const char *name) {
    static const PTZCommand commands[] = {
        {"up", DH_PTZ_UP_CONTROL, 0, 1},
        {"down", DH_PTZ_DOWN_CONTROL, 0, 1},
        {"left", DH_PTZ_LEFT_CONTROL, 1, 0},
        {"right", DH_PTZ_RIGHT_CONTROL, 1, 0},
        {"zoom_in", DH_PTZ_ZOOM_ADD_CONTROL, 0, 1},
        {"zoom_out", DH_PTZ_ZOOM_DEC_CONTROL, 0, 1},
        {"lefttop", DH_EXTPTZ_LEFTTOP, 1, 1},
        {"leftdown", DH_EXTPTZ_LEFTDOWN, 1, 1},
    };
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        if (strcmp(commands[i].name, name) == 0) {
            return &commands[i];
        }
    }
    return NULL;
}

static int64_t monotonic_millis(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
        return 0;
    }
    return (int64_t)value.tv_sec * 1000LL + (int64_t)value.tv_nsec / 1000000LL;
}

static LLONG login_camera(SDK *sdk, const Options *opt, char *errbuf, size_t errbuf_size) {
    NET_DEVICEINFO_Ex info;
    memset(&info, 0, sizeof(info));
    int login_error = 0;
    LLONG login = sdk->CLIENT_LoginEx2(
        opt->host,
        (uint16_t)opt->port,
        opt->user,
        opt->password,
        EM_LOGIN_SPEC_CAP_TCP,
        NULL,
        &info,
        &login_error
    );
    if (!login) {
        snprintf(
            errbuf,
            errbuf_size,
            "CLIENT_LoginEx2 failed, sdk_error=0x%08x, login_error=%d",
            sdk->CLIENT_GetLastError(),
            login_error
        );
    }
    return login;
}

static int perform_ptz(SDK *sdk, LLONG login, const Options *opt, PTZResult *result, char *errbuf, size_t errbuf_size) {
    memset(result, 0, sizeof(*result));
    static PTZCommand raw_command;
    if (opt->raw_ptz_code >= 0) {
        raw_command.name = opt->command ? opt->command : "raw";
        raw_command.code = (DWORD)opt->raw_ptz_code;
        raw_command.use_param1 = 0;
        raw_command.use_param2 = 0;
        result->ptz = &raw_command;
    } else {
        result->ptz = find_ptz_command(opt->command);
    }
    if (!result->ptz) {
        snprintf(errbuf, errbuf_size, "unknown PTZ command");
        return -1;
    }
    if (strcmp(opt->action, "start") != 0 && strcmp(opt->action, "stop") != 0 && strcmp(opt->action, "pulse") != 0) {
        snprintf(errbuf, errbuf_size, "unknown PTZ action");
        return -1;
    }
    result->pulse = strcmp(opt->action, "pulse") == 0;
    result->moving = strcmp(opt->action, "start") == 0 || result->pulse;
    if (opt->raw_ptz_code >= 0) {
        result->param1 = result->moving ? opt->raw_param1 : 0;
        result->param2 = result->moving ? opt->raw_param2 : 0;
    } else {
        result->param1 = result->moving && result->ptz->use_param1 ? opt->speed : 0;
        result->param2 = result->moving && result->ptz->use_param2 ? opt->speed : 0;
    }
    result->stop = result->moving ? 0 : 1;
    int prefer_basic_api = 0;
    int prefer_ex2_api =
        (result->ptz->code == DH_PTZ_UP_CONTROL ||
         result->ptz->code == DH_PTZ_DOWN_CONTROL ||
         result->ptz->code == DH_EXTPTZ_LEFTTOP ||
         result->ptz->code == DH_EXTPTZ_LEFTDOWN) &&
        sdk->CLIENT_DHPTZControlEx2;
    BOOL ok = 0;
    DWORD first_error = 0;
    DWORD second_error = 0;
    DWORD ex2_error = 0;
    if (prefer_ex2_api) {
        result->control_api = "CLIENT_DHPTZControlEx2";
        ok = sdk->CLIENT_DHPTZControlEx2(
            login,
            opt->channel,
            result->ptz->code,
            result->param1,
            result->param2,
            0,
            result->stop,
            NULL
        );
        ex2_error = ok ? 0 : sdk->CLIENT_GetLastError();
    }
    if (prefer_basic_api && sdk->CLIENT_PTZControl) {
        result->control_api = "CLIENT_PTZControl";
        ok = sdk->CLIENT_PTZControl(login, opt->channel, result->ptz->code, (DWORD)opt->speed, (DWORD)result->stop);
        first_error = ok ? 0 : sdk->CLIENT_GetLastError();
    }
    if (!ok) {
        result->control_api = "CLIENT_DHPTZControlEx";
        ok = sdk->CLIENT_DHPTZControlEx(
            login,
            opt->channel,
            result->ptz->code,
            result->param1,
            result->param2,
            0,
            result->stop
        );
        second_error = ok ? 0 : sdk->CLIENT_GetLastError();
    }
    if (!ok && !prefer_basic_api && sdk->CLIENT_PTZControl) {
        first_error = second_error;
        result->control_api = "CLIENT_PTZControl";
        ok = sdk->CLIENT_PTZControl(login, opt->channel, result->ptz->code, (DWORD)opt->speed, (DWORD)result->stop);
        second_error = ok ? 0 : sdk->CLIENT_GetLastError();
    }
    if (!ok) {
        if (prefer_ex2_api && sdk->CLIENT_PTZControl) {
            snprintf(
                errbuf,
                errbuf_size,
                "CLIENT_DHPTZControlEx2 failed, last_error=0x%08x; %s failed, last_error=0x%08x; %s failed, last_error=0x%08x",
                ex2_error,
                prefer_basic_api ? "CLIENT_PTZControl" : "CLIENT_DHPTZControlEx",
                first_error,
                prefer_basic_api ? "CLIENT_DHPTZControlEx" : "CLIENT_PTZControl",
                second_error
            );
        } else if (prefer_ex2_api) {
            snprintf(
                errbuf,
                errbuf_size,
                "CLIENT_DHPTZControlEx2 failed, last_error=0x%08x; CLIENT_DHPTZControlEx failed, last_error=0x%08x; CLIENT_PTZControl unavailable",
                ex2_error,
                first_error
            );
        } else if (sdk->CLIENT_PTZControl) {
            snprintf(
                errbuf,
                errbuf_size,
                "%s failed, last_error=0x%08x; %s failed, last_error=0x%08x",
                prefer_basic_api ? "CLIENT_PTZControl" : "CLIENT_DHPTZControlEx",
                first_error,
                prefer_basic_api ? "CLIENT_DHPTZControlEx" : "CLIENT_PTZControl",
                second_error
            );
        } else {
            snprintf(
                errbuf,
                errbuf_size,
                "CLIENT_DHPTZControlEx failed, last_error=0x%08x; CLIENT_PTZControl unavailable",
                first_error
            );
        }
        return -1;
    }
    result->pulse_stop_ok = 1;
    result->pulse_stop_error = 0;
    if (result->pulse) {
        usleep((useconds_t)opt->duration_ms * 1000U);
        result->pulse_stop_ok = 0;
        if (strcmp(result->control_api, "CLIENT_DHPTZControlEx2") == 0 && sdk->CLIENT_DHPTZControlEx2) {
            result->pulse_stop_ok = sdk->CLIENT_DHPTZControlEx2(
                login,
                opt->channel,
                result->ptz->code,
                0,
                0,
                0,
                1,
                NULL
            );
            result->pulse_stop_error = result->pulse_stop_ok ? 0 : sdk->CLIENT_GetLastError();
        }
        if (!result->pulse_stop_ok && strcmp(result->control_api, "CLIENT_PTZControl") == 0 && sdk->CLIENT_PTZControl) {
            result->pulse_stop_ok = sdk->CLIENT_PTZControl(login, opt->channel, result->ptz->code, (DWORD)opt->speed, 1);
            result->pulse_stop_error = result->pulse_stop_ok ? 0 : sdk->CLIENT_GetLastError();
        }
        if (!result->pulse_stop_ok) {
            result->pulse_stop_ok = sdk->CLIENT_DHPTZControlEx(
                login,
                opt->channel,
                result->ptz->code,
                0,
                0,
                0,
                1
            );
            result->pulse_stop_error = result->pulse_stop_ok ? 0 : sdk->CLIENT_GetLastError();
        }
        if (!result->pulse_stop_ok && sdk->CLIENT_PTZControl) {
            result->pulse_stop_ok = sdk->CLIENT_PTZControl(login, opt->channel, result->ptz->code, (DWORD)opt->speed, 1);
            result->pulse_stop_error = result->pulse_stop_ok ? 0 : sdk->CLIENT_GetLastError();
        }
        if (!result->pulse_stop_ok) {
            snprintf(errbuf, errbuf_size, "PTZ pulse stop failed, last_error=0x%08x", result->pulse_stop_error);
            return -1;
        }
    }
    return 0;
}

static void emit_command_error(const char *message, const Options *opt, int persistent, int reconnected) {
    printf("{\"status\":\"error\",\"event\":\"completed\",\"backend\":\"x64_netsdk_qemu%s\",\"error\":\"",
           persistent ? "_persistent" : "");
    json_escape(message);
    printf("\",\"command\":\"");
    json_escape(opt->command ? opt->command : "");
    printf("\",\"action\":\"");
    json_escape(opt->action ? opt->action : "");
    printf("\",\"reconnected\":%s}\n", reconnected ? "true" : "false");
    fflush(stdout);
}

static void emit_command_success(
    const Options *opt,
    const PTZResult *result,
    int native_open,
    int persistent,
    int reconnected,
    int64_t latency_ms
) {
    printf("{\"status\":\"ok\",\"event\":\"completed\",\"backend\":\"x64_netsdk_qemu%s\",\"host\":\"",
           persistent ? "_persistent" : "");
    json_escape(opt->host);
    printf("\",\"port\":%d,\"channel\":%d,\"command\":\"", opt->port, opt->channel);
    json_escape(opt->command);
    printf("\",\"action\":\"");
    json_escape(opt->action);
    printf("\",\"speed\":%d,\"ptz_code\":%u,\"param1\":%d,\"param2\":%d,\"stop\":%s,\"control_api\":\"",
           opt->speed,
           result->ptz->code,
           result->param1,
           result->param2,
           result->stop ? "true" : "false");
    json_escape(result->control_api);
    printf("\",\"duration_ms\":%d,\"pulse_stop_ok\":%s,\"pulse_stop_error\":\"0x%08x\",\"native_port_open\":%s,\"persistent\":%s,\"reconnected\":%s,\"latency_ms\":%lld,\"sdk_lib\":\"",
           result->pulse ? opt->duration_ms : 0,
           result->pulse_stop_ok ? "true" : "false",
           result->pulse_stop_error,
           native_open ? "true" : "false",
           persistent ? "true" : "false",
           reconnected ? "true" : "false",
           (long long)latency_ms);
    json_escape(opt->sdk_lib);
    printf("\"}\n");
    fflush(stdout);
}

static void clamp_command_options(Options *opt) {
    if (opt->speed < 1) {
        opt->speed = 1;
    } else if (opt->speed > 8) {
        opt->speed = 8;
    }
    if (opt->duration_ms < 30) {
        opt->duration_ms = 30;
    } else if (opt->duration_ms > 1000) {
        opt->duration_ms = 1000;
    }
}

static int run_stdin_server(SDK *sdk, LLONG *login, const Options *base, int native_open) {
    char line[256];
    printf("{\"status\":\"ok\",\"event\":\"ready\",\"backend\":\"x64_netsdk_qemu_persistent\",\"host\":\"");
    json_escape(base->host);
    printf("\",\"port\":%d,\"channel\":%d,\"native_port_open\":%s}\n",
           base->port,
           base->channel,
           native_open ? "true" : "false");
    fflush(stdout);

    while (fgets(line, sizeof(line), stdin)) {
        char command[32] = {0};
        char action[16] = {0};
        char trailing = 0;
        int speed = 1;
        int duration_ms = 120;
        Options request = *base;
        int parsed = sscanf(line, "%31s %15s %d %d %c", command, action, &speed, &duration_ms, &trailing);
        if (parsed != 4) {
            request.command = "";
            request.action = "";
            emit_command_error("invalid persistent PTZ command", &request, 1, 0);
            continue;
        }
        request.command = command;
        request.action = action;
        request.speed = speed;
        request.duration_ms = duration_ms;
        clamp_command_options(&request);
        char errbuf[1024] = {0};
        PTZResult result;
        int reconnected = 0;
        int64_t started_ms = monotonic_millis();
        int rc = perform_ptz(sdk, *login, &request, &result, errbuf, sizeof(errbuf));
        if (rc != 0) {
            if (*login) {
                sdk->CLIENT_Logout(*login);
                *login = 0;
            }
            *login = login_camera(sdk, base, errbuf, sizeof(errbuf));
            if (*login) {
                reconnected = 1;
                rc = perform_ptz(sdk, *login, &request, &result, errbuf, sizeof(errbuf));
            }
        }
        if (rc != 0) {
            emit_command_error(errbuf[0] ? errbuf : "persistent native PTZ command failed", &request, 1, reconnected);
            continue;
        }
        emit_command_success(
            &request,
            &result,
            native_open,
            1,
            reconnected,
            monotonic_millis() - started_ms
        );
    }
    return 0;
}

int main(int argc, char **argv) {
    Options opt;
    if (parse_options(argc, argv, &opt) != 0) {
        return emit_error("invalid arguments", &opt, 0, opt.sdk_lib);
    }
    int native_open = tcp_port_open(opt.host, opt.port, opt.timeout_seconds > 2 ? 2 : opt.timeout_seconds);
    void *handle = NULL;
    SDK sdk;
    memset(&sdk, 0, sizeof(sdk));
    char errbuf[1024] = {0};
    if (load_sdk(opt.sdk_lib, &handle, &sdk, errbuf, sizeof(errbuf)) != 0) {
        return emit_error(errbuf, &opt, native_open, opt.sdk_lib);
    }

    LLONG login = 0;
    int initialized = 0;
    int exit_code = 1;
    do {
        if (!sdk.CLIENT_Init(disconnect_cb, 0)) {
            snprintf(errbuf, sizeof(errbuf), "CLIENT_Init failed, last_error=0x%08x", sdk.CLIENT_GetLastError());
            break;
        }
        initialized = 1;
        if (sdk.CLIENT_SetConnectTime) {
            sdk.CLIENT_SetConnectTime(opt.timeout_seconds * 1000, 1);
        }
        login = login_camera(&sdk, &opt, errbuf, sizeof(errbuf));
        if (!login) {
            break;
        }
        if (opt.stdin_server) {
            exit_code = run_stdin_server(&sdk, &login, &opt, native_open);
            break;
        }
        PTZResult result;
        int64_t started_ms = monotonic_millis();
        if (perform_ptz(&sdk, login, &opt, &result, errbuf, sizeof(errbuf)) != 0) {
            break;
        }
        emit_command_success(&opt, &result, native_open, 0, 0, monotonic_millis() - started_ms);
        exit_code = 0;
    } while (0);

    if (login) {
        sdk.CLIENT_Logout(login);
    }
    if (initialized) {
        sdk.CLIENT_Cleanup();
    }
    if (handle) {
        dlclose(handle);
    }
    if (exit_code != 0) {
        return emit_error(errbuf[0] ? errbuf : "native x64 PTZ helper failed", &opt, native_open, opt.sdk_lib);
    }
    return 0;
}
