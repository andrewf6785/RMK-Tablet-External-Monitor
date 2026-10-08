#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

#define PATH_CAP 32768
#define ID_START 201
#define ID_STOP 202
#define ID_CONFIGURE 203
#define ID_DIAGNOSE 204
#define ID_LOG 205
#define ID_SKIP 206
#define ID_FREEZE 207
#define ID_PRESERVE 208
#define ID_FAST_CAPTURE 209
#define ID_TRANSPARENT 210
#define ID_RESET_SLEEP 211
#define WM_OUTPUT (WM_APP + 1)
#define WM_DONE (WM_APP + 2)
#define WM_PHASE (WM_APP + 3)
#define WM_AUTOSTART (WM_APP + 4)
#define WM_CONTROL_DONE (WM_APP + 5)
#define WM_RELAUNCH (WM_APP + 6)
#define MODE_NORMAL 0
#define MODE_CONFIGURE 1
#define MODE_DIAGNOSE 2
#define MODE_FREEZE 3
#define MODE_PRESERVE 4
#define MODE_RESET_SLEEP 5
#define MODE_SLEEP 6

static const wchar_t CLASS_NAME[] = L"RemarkableMonitorLauncherWindow";
static wchar_t directory[PATH_CAP], script_path[PATH_CAP], log_path[PATH_CAP], freeze_path[PATH_CAP];
static HWND window, output, status, start_button, stop_button, configure_button;
static HWND diagnose_button, log_button, skip_box, note;
static HWND freeze_button, preserve_button, reset_sleep_button;
static HWND fast_capture_box, transparent_box;
static HANDLE job, log_file = INVALID_HANDLE_VALUE;
static HANDLE control_job;
static BOOL running, stopping;
static BOOL control_running, close_after_control, frozen, sleep_failed;
static int monitor_mode;
static HFONT log_font;
static HINSTANCE instance;

typedef struct {
    HWND target;
    HANDLE process, read_pipe;
    int mode;
} Reader;

static void file_path(wchar_t *destination, const wchar_t *name) {
    _snwprintf(destination, PATH_CAP, L"%ls\\%ls", directory, name);
    destination[PATH_CAP - 1] = 0;
}

static void append_output(const wchar_t *message) {
    LRESULT length = GetWindowTextLengthW(output);
    if (length > 160000) {
        SendMessageW(output, EM_SETSEL, 0, 60000);
        SendMessageW(output, EM_REPLACESEL, FALSE, (LPARAM)L"");
        length = GetWindowTextLengthW(output);
    }
    SendMessageW(output, EM_SETSEL, length, length);
    SendMessageW(output, EM_REPLACESEL, FALSE, (LPARAM)message);
    SendMessageW(output, EM_SCROLLCARET, 0, 0);
    if (log_file != INVALID_HANDLE_VALUE) {
        int bytes = WideCharToMultiByte(CP_UTF8, 0, message, -1, NULL, 0, NULL, NULL);
        char *utf8 = bytes > 0 ? malloc((size_t)bytes) : NULL;
        if (utf8) {
            DWORD written;
            WideCharToMultiByte(CP_UTF8, 0, message, -1, utf8, bytes, NULL, NULL);
            WriteFile(log_file, utf8, (DWORD)(bytes - 1), &written, NULL);
            free(utf8);
        }
    }
}

static void report_error(const wchar_t *action, DWORD code) {
    wchar_t description[1024] = L"", message[1400];
    FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                   NULL, code, 0, description, 1024, NULL);
    _snwprintf(message, 1400, L"%ls (Windows error %lu): %ls\r\n", action,
               (unsigned long)code, description);
    message[1399] = 0;
    append_output(message);
    SetWindowTextW(status, L"Could not start. See the log below.");
}

static void set_controls(BOOL active) {
    EnableWindow(start_button, !active && !control_running);
    EnableWindow(stop_button, !stopping && !control_running);
    EnableWindow(configure_button, !active && !control_running);
    EnableWindow(diagnose_button, !active && !control_running);
    EnableWindow(skip_box, !active && !control_running);
    EnableWindow(fast_capture_box, !active && !control_running);
    EnableWindow(transparent_box, !active && !control_running);
    EnableWindow(freeze_button, active && monitor_mode != MODE_DIAGNOSE && !stopping && !control_running && !frozen);
    EnableWindow(preserve_button, active && monitor_mode != MODE_DIAGNOSE && !stopping && !control_running && !frozen);
    EnableWindow(reset_sleep_button, !stopping && !control_running && (!active || monitor_mode != MODE_DIAGNOSE));
}

static void post_utf8(HWND target, const char *bytes, int count) {
    int size = MultiByteToWideChar(CP_UTF8, 0, bytes, count, NULL, 0);
    if (size <= 0) return;
    /* Convert LF to CRLF for the Windows multiline edit control. */
    wchar_t *decoded = calloc((size_t)size + 1, sizeof(wchar_t));
    wchar_t *display = calloc((size_t)size * 2 + 1, sizeof(wchar_t));
    if (!decoded || !display) { free(decoded); free(display); return; }
    MultiByteToWideChar(CP_UTF8, 0, bytes, count, decoded, size);
    int j = 0;
    for (int i = 0; i < size; ++i) {
        if (decoded[i] == L'\r') continue;
        if (decoded[i] == L'\n') display[j++] = L'\r';
        display[j++] = decoded[i];
    }
    free(decoded);
    if (!PostMessageW(target, WM_OUTPUT, 0, (LPARAM)display)) free(display);
}

static int complete_utf8(const char *bytes, int size) {
    if (!size) return 0;
    int start = size - 1;
    while (start > 0 && ((unsigned char)bytes[start] & 0xc0) == 0x80) --start;
    unsigned char first = (unsigned char)bytes[start];
    int needed = first >= 0xf0 ? 4 : first >= 0xe0 ? 3 : first >= 0xc0 ? 2 : 1;
    return size - start < needed ? start : size;
}

static void detect_phase(HWND target, const char *bytes, char *tail) {
    char combined[4352];
    size_t previous = strlen(tail);
    _snprintf(combined, sizeof(combined), "%s%s", tail, bytes);
    combined[sizeof(combined)-1] = 0;
    const char *patterns[] = {"Cursor proxy ready at", "Tablet connected:", "Tablet disconnected."};
    const char *latest = NULL;
    int phase = 0;
    for (int i = 0; i < 3; ++i) {
        const char *found = combined;
        while ((found = strstr(found, patterns[i])) != NULL) {
            if ((size_t)(found - combined) + strlen(patterns[i]) > previous &&
                (!latest || found > latest)) { latest = found; phase = i + 1; }
            ++found;
        }
    }
    if (phase) PostMessageW(target, WM_PHASE, (WPARAM)phase, 0);
    size_t length = strlen(combined);
    strcpy(tail, combined + (length > 127 ? length - 127 : 0));
}

static DWORD WINAPI read_child(LPVOID data) {
    Reader *reader = data;
    char buffer[4101], tail[128] = "";
    DWORD count, code = 1;
    int carry = 0;
    if (reader->read_pipe) {
        while (ReadFile(reader->read_pipe, buffer + carry, 4096, &count, NULL) && count) {
            int total = carry + (int)count;
            buffer[total] = 0;
            int complete = complete_utf8(buffer, total);
            char saved = buffer[complete];
            buffer[complete] = 0;
            if (reader->mode == MODE_NORMAL || reader->mode == MODE_CONFIGURE)
                detect_phase(reader->target, buffer, tail);
            buffer[complete] = saved;
            post_utf8(reader->target, buffer, complete);
            carry = total - complete;
            memmove(buffer, buffer + complete, (size_t)carry);
        }
        if (carry) post_utf8(reader->target, buffer, carry);
        CloseHandle(reader->read_pipe);
    }
    WaitForSingleObject(reader->process, INFINITE);
    GetExitCodeProcess(reader->process, &code);
    CloseHandle(reader->process);
    PostMessageW(reader->target, reader->mode >= MODE_FREEZE ? WM_CONTROL_DONE : WM_DONE,
                 (WPARAM)code, (LPARAM)reader->mode);
    free(reader);
    return 0;
}

static void launch(int mode) {
    BOOL tablet_control = mode >= MODE_FREEZE;
    if (control_running || stopping || (!tablet_control && running)) return;
    if (tablet_control && mode != MODE_SLEEP && running && monitor_mode == MODE_DIAGNOSE) return;
    const wchar_t *selected_script = tablet_control ? freeze_path : script_path;
    if (GetFileAttributesW(selected_script) == INVALID_FILE_ATTRIBUTES) {
        append_output(L"A required Python script is missing. Extract the entire ZIP beside this executable.\r\n");
        SetWindowTextW(status, L"Python files are missing.");
        return;
    }
    wchar_t python[PATH_CAP], command[PATH_CAP * 2 + 128];
    DWORD found = SearchPathW(NULL, L"py.exe", NULL, PATH_CAP, python, NULL);
    if (!found || found >= PATH_CAP) {
        append_output(L"The Windows Python launcher (py.exe) was not found. Install Python with its launcher, then retry.\r\n");
        SetWindowTextW(status, L"Python launcher not found.");
        return;
    }
    const wchar_t *option = mode == MODE_FREEZE ? L" freeze --launcher" :
                            mode == MODE_PRESERVE ? L" preserve --launcher" :
                            mode == MODE_RESET_SLEEP ? L" reset --launcher" :
                            mode == MODE_SLEEP ? L" sleep --launcher" :
                            mode == MODE_CONFIGURE ? L" --configure" :
                            mode == MODE_DIAGNOSE ? L" --diagnose" : L"";
    const wchar_t *skip = !tablet_control && mode != MODE_DIAGNOSE &&
        SendMessageW(skip_box, BM_GETCHECK, 0, 0) == BST_CHECKED ? L" --skip-restart" : L"";
    const wchar_t *fast_capture = !tablet_control && mode != MODE_DIAGNOSE &&
        SendMessageW(fast_capture_box, BM_GETCHECK, 0, 0) == BST_CHECKED ? L" --fast-capture" : L"";
    const wchar_t *transparent = !tablet_control && mode != MODE_DIAGNOSE &&
        SendMessageW(transparent_box, BM_GETCHECK, 0, 0) == BST_CHECKED ? L" --capture-transparent-windows" : L"";
    _snwprintf(command, sizeof(command) / sizeof(command[0]),
               L"\"%ls\" -3 -u \"%ls\"%ls%ls%ls%ls", python, selected_script, option, skip, fast_capture, transparent);
    command[sizeof(command) / sizeof(command[0]) - 1] = 0;
    if (!tablet_control && mode != MODE_DIAGNOSE) { frozen = FALSE; sleep_failed = FALSE; }

    HANDLE read_pipe = NULL, write_pipe = NULL, input_null = INVALID_HANDLE_VALUE;
    SECURITY_ATTRIBUTES security = {sizeof(security), NULL, TRUE};
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    startup.cb = sizeof(startup);
    DWORD flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT;
    if (mode == MODE_CONFIGURE) {
        flags |= CREATE_NEW_CONSOLE;
    } else {
        flags |= CREATE_NO_WINDOW;
        if (!CreatePipe(&read_pipe, &write_pipe, &security, 0)) {
            report_error(L"Creating the output pipe failed", GetLastError()); return;
        }
        if (!SetHandleInformation(read_pipe, HANDLE_FLAG_INHERIT, 0)) {
            DWORD error = GetLastError();
            CloseHandle(read_pipe); CloseHandle(write_pipe);
            report_error(L"Preparing the output pipe failed", error); return;
        }
        input_null = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 &security, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (input_null == INVALID_HANDLE_VALUE) {
            DWORD error = GetLastError();
            CloseHandle(read_pipe); CloseHandle(write_pipe);
            report_error(L"Preparing standard input failed", error); return;
        }
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = input_null;
        startup.hStdOutput = startup.hStdError = write_pipe;
    }
    HANDLE new_job = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                                              JOB_OBJECT_LIMIT_BREAKAWAY_OK;
    if (!new_job || !SetInformationJobObject(new_job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        DWORD error = GetLastError();
        if (new_job) CloseHandle(new_job);
        if (read_pipe) CloseHandle(read_pipe);
        if (write_pipe) CloseHandle(write_pipe);
        if (input_null != INVALID_HANDLE_VALUE) CloseHandle(input_null);
        report_error(L"Creating the Python process job failed", error); return;
    }
    SetEnvironmentVariableW(L"REMARKABLE_LAUNCHER_JOB", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    BOOL created = CreateProcessW(python, command, NULL, NULL, mode != MODE_CONFIGURE,
                                 flags, NULL, directory, &startup, &process);
    DWORD error = created ? ERROR_SUCCESS : GetLastError();
    if (write_pipe) CloseHandle(write_pipe);
    if (input_null != INVALID_HANDLE_VALUE) CloseHandle(input_null);
    if (!created || !AssignProcessToJobObject(new_job, process.hProcess)) {
        if (created) {
            error = GetLastError();
            TerminateProcess(process.hProcess, 1);
            CloseHandle(process.hThread); CloseHandle(process.hProcess);
        }
        CloseHandle(new_job);
        if (read_pipe) CloseHandle(read_pipe);
        report_error(L"Starting the managed Python process failed", error); return;
    }
    Reader *reader = calloc(1, sizeof(Reader));
    if (reader) {
        reader->target = window; reader->process = process.hProcess;
        reader->read_pipe = read_pipe; reader->mode = mode;
    }
    HANDLE thread = reader ? CreateThread(NULL, 0, read_child, reader, 0, NULL) : NULL;
    if (!thread) {
        error = reader ? GetLastError() : ERROR_NOT_ENOUGH_MEMORY;
        CloseHandle(new_job);
        CloseHandle(process.hThread); CloseHandle(process.hProcess);
        if (read_pipe) CloseHandle(read_pipe);
        free(reader);
        report_error(L"Starting the output reader failed", error); return;
    }
    CloseHandle(thread);
    if (tablet_control) {
        control_job = new_job;
        control_running = TRUE;
    } else {
        job = new_job;
        running = TRUE; stopping = FALSE;
        monitor_mode = mode;
    }
    set_controls(running);
    SetWindowTextW(status, mode == MODE_FREEZE ? L"Saving the current tablet picture for this sleep session..." :
                   mode == MODE_PRESERVE ? L"Saving the current picture as the tablet's persistent sleep screen..." :
                   mode == MODE_RESET_SLEEP ? L"Restoring the original tablet sleep screen..." :
                   mode == MODE_SLEEP ? L"Requesting tablet sleep, then stopping the PC monitor session..." :
                   mode == MODE_DIAGNOSE ? L"Reading diagnostics..." :
                   mode == MODE_CONFIGURE ? L"Configuring the tablet; SSH key setup is automatic if needed." :
                   L"Starting: verifying the display, TightVNC and cursor proxy...");
    append_output(mode == MODE_FREEZE ? L"\r\nFreeze: save the tablet's current pixels as a temporary sleep image, then request native sleep.\r\n" :
                  mode == MODE_PRESERVE ? L"\r\nFreeze and preserve: save the tablet's current pixels for future sleep screens, then request native sleep.\r\n" :
                  mode == MODE_RESET_SLEEP ? L"\r\nReset: remove this app's sleep-image overrides and restore the original sleep behavior.\r\n" :
                  mode == MODE_SLEEP ? L"\r\nStop: request native tablet sleep using its current sleep screen.\r\n" :
                  mode == MODE_CONFIGURE ? L"\r\nTablet configuration: the saved SSH key is reused; first-time setup may ask for the password once.\r\n" :
                  mode == MODE_DIAGNOSE ? L"\r\nReading diagnostics (no display changes).\r\n" :
                  L"\r\nStarting the monitor. Minimize this window to keep it running.\r\n");
    if (ResumeThread(process.hThread) == (DWORD)-1) {
        report_error(L"Resuming Python failed", GetLastError());
        TerminateJobObject(new_job, 1);
    }
    CloseHandle(process.hThread);
}

static void stop_monitor(void) {
    if (running && job && !stopping) {
        stopping = TRUE;
        if (!frozen) SetWindowTextW(status, L"Stopping this launcher's Python processes...");
        set_controls(TRUE);
        if (!TerminateJobObject(job, 0)) {
            stopping = FALSE;
            report_error(L"Stopping Python failed", GetLastError());
            set_controls(TRUE);
        }
    }
}

static HWND control(const wchar_t *type, const wchar_t *text, DWORD style, int id) {
    HWND handle = CreateWindowExW(0, type, text, WS_CHILD | WS_VISIBLE | style,
                                 0, 0, 1, 1, window, (HMENU)(INT_PTR)id, instance, NULL);
    SendMessageW(handle, WM_SETFONT, (WPARAM)GetStockObject(DEFAULT_GUI_FONT), TRUE);
    return handle;
}

static void layout(void) {
    RECT client; GetClientRect(window, &client);
    int width = client.right;
    MoveWindow(start_button, 12, 12, 104, 30, TRUE);
    MoveWindow(stop_button, 124, 12, 72, 30, TRUE);
    MoveWindow(configure_button, 204, 12, 144, 30, TRUE);
    MoveWindow(diagnose_button, 356, 12, 100, 30, TRUE);
    MoveWindow(log_button, 464, 12, 94, 30, TRUE);
    MoveWindow(freeze_button, 12, 52, 104, 30, TRUE);
    MoveWindow(preserve_button, 124, 52, 316, 30, TRUE);
    MoveWindow(reset_sleep_button, 448, 52, 236, 30, TRUE);
    MoveWindow(skip_box, 12, 88, 210, 24, TRUE);
    MoveWindow(fast_capture_box, 230, 88, width - 242, 24, TRUE);
    MoveWindow(transparent_box, 12, 116, width - 24, 24, TRUE);
    MoveWindow(status, 12, 152, width - 24, 38, TRUE);
    MoveWindow(note, 12, 196, width - 24, 30, TRUE);
    MoveWindow(output, 12, 234, width - 24, client.bottom > 252 ? client.bottom - 246 : 1, TRUE);
}

static LRESULT CALLBACK window_proc(HWND target, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case WM_CREATE:
        window = target;
        start_button = control(L"BUTTON", L"Start / retry", BS_PUSHBUTTON | WS_TABSTOP, ID_START);
        stop_button = control(L"BUTTON", L"Stop", BS_PUSHBUTTON | WS_TABSTOP, ID_STOP);
        configure_button = control(L"BUTTON", L"Configure tablet...", BS_PUSHBUTTON | WS_TABSTOP, ID_CONFIGURE);
        diagnose_button = control(L"BUTTON", L"Diagnostics", BS_PUSHBUTTON | WS_TABSTOP, ID_DIAGNOSE);
        log_button = control(L"BUTTON", L"Open log", BS_PUSHBUTTON | WS_TABSTOP, ID_LOG);
        freeze_button = control(L"BUTTON", L"Freeze", BS_PUSHBUTTON | WS_TABSTOP, ID_FREEZE);
        preserve_button = control(L"BUTTON", L"Freeze and preserve as sleep screen", BS_PUSHBUTTON | WS_TABSTOP, ID_PRESERVE);
        reset_sleep_button = control(L"BUTTON", L"Reset to default sleep screen", BS_PUSHBUTTON | WS_TABSTOP, ID_RESET_SLEEP);
        skip_box = control(L"BUTTON", L"Skip driver restart", BS_AUTOCHECKBOX | WS_TABSTOP, ID_SKIP);
        fast_capture_box = control(L"BUTTON", L"Fast capture (more desktop load)", BS_AUTOCHECKBOX | WS_TABSTOP, ID_FAST_CAPTURE);
        transparent_box = control(L"BUTTON", L"Include transparent overlays", BS_AUTOCHECKBOX | WS_TABSTOP, ID_TRANSPARENT);
        status = control(L"STATIC", L"Ready.", 0, 0);
        note = control(L"STATIC", L"Freeze sleeps with the current picture. Wake the tablet and open AppLoad yourself.", 0, 0);
        output = control(L"EDIT", L"", ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL |
                         ES_AUTOHSCROLL | WS_VSCROLL | WS_HSCROLL | WS_BORDER | WS_TABSTOP, 0);
        log_font = CreateFontW(-14, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                              DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                              CLEARTYPE_QUALITY, FIXED_PITCH, L"Consolas");
        if (log_font) SendMessageW(output, WM_SETFONT, (WPARAM)log_font, TRUE);
        SendMessageW(output, EM_SETLIMITTEXT, 250000, 0);
        set_controls(FALSE);
        return 0;
    case WM_SIZE: layout(); return 0;
    case WM_GETMINMAXINFO:
        ((MINMAXINFO *)lparam)->ptMinTrackSize.x = 720;
        ((MINMAXINFO *)lparam)->ptMinTrackSize.y = 410;
        return 0;
    case WM_AUTOSTART: launch(MODE_NORMAL); return 0;
    case WM_RELAUNCH:
        return 0;
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case ID_START: launch(MODE_NORMAL); break;
        case ID_CONFIGURE: launch(MODE_CONFIGURE); break;
        case ID_DIAGNOSE: launch(MODE_DIAGNOSE); break;
        case ID_FREEZE: if (running && !frozen) launch(MODE_FREEZE); break;
        case ID_PRESERVE: if (running && !frozen) launch(MODE_PRESERVE); break;
        case ID_RESET_SLEEP: launch(MODE_RESET_SLEEP); break;
        case ID_STOP:
            if (!control_running) {
                launch(MODE_SLEEP);
                if (!control_running) {
                    sleep_failed = TRUE;
                    append_output(L"The sleep helper could not start. Stopping the PC session; put the tablet to sleep yourself.\r\n");
                    stop_monitor();
                    SetWindowTextW(status, L"PC session stopped; tablet sleep was not confirmed. See the error below.");
                }
            }
            break;
        case ID_LOG:
            if (log_file != INVALID_HANDLE_VALUE) FlushFileBuffers(log_file);
            ShellExecuteW(window, L"open", log_path, NULL, directory, SW_SHOWNORMAL);
            break;
        }
        return 0;
    case WM_OUTPUT:
        append_output((wchar_t *)lparam); free((void *)lparam); return 0;
    case WM_PHASE:
        if (!stopping && !control_running && !frozen)
            SetWindowTextW(status, wparam == 2 ? L"Tablet connected. Proxy running." :
                                      L"Proxy ready. Open VNSee on the tablet.");
        return 0;
    case WM_CONTROL_DONE:
        if (control_job) { CloseHandle(control_job); control_job = NULL; }
        control_running = FALSE;
        if (wparam) {
            append_output(L"Tablet command was not confirmed. Read remarkable_sleep.log before retrying.\r\n");
            SetWindowTextW(status, L"Tablet action failed. See remarkable_sleep.log.");
            if (lparam == MODE_SLEEP) {
                sleep_failed = TRUE;
                append_output(L"Stopping the PC session; tablet sleep could not be confirmed.\r\n");
                stop_monitor();
                SetWindowTextW(status, L"PC session stopped; tablet sleep was not confirmed. See remarkable_sleep.log.");
            }
            if (close_after_control)
                append_output(L"The window was kept open because the tablet command failed.\r\n");
            close_after_control = FALSE;
            set_controls(running);
            return 0;
        }
        if (lparam == MODE_FREEZE || lparam == MODE_PRESERVE || lparam == MODE_SLEEP) {
            frozen = TRUE;
            sleep_failed = FALSE;
            append_output(L"The tablet accepted the sleep request. Disconnecting this launcher's VNC proxy; USB remains connected.\r\n");
            SetWindowTextW(status, L"Sleep requested. You can close this window. Wake the tablet yourself when needed.");
            stop_monitor();
        } else {
            append_output(L"The original tablet sleep screen is restored.\r\n");
            SetWindowTextW(status, L"Default sleep screen restored.");
        }
        set_controls(running);
        if (close_after_control) {
            close_after_control = FALSE;
            SendMessageW(window, WM_CLOSE, 0, 0);
        }
        return 0;
    case WM_DONE: {
        wchar_t result[200];
        if (job) { CloseHandle(job); job = NULL; }
        running = FALSE;
        if (frozen) {
            SetWindowTextW(status, L"Sleep requested; PC session stopped. Wake the tablet yourself before using it again.");
            append_output(L"The Windows monitor session ended. No wake or AppLoad commands will be sent.\r\n");
        } else if (control_running) {
            SetWindowTextW(status, L"Monitor session ended; the tablet command is still running.");
            append_output(L"The monitor session ended while checking the tablet.\r\n");
        } else if (sleep_failed) {
            SetWindowTextW(status, L"PC session stopped; tablet sleep was not confirmed. See remarkable_sleep.log.");
            append_output(L"This launcher's Python processes stopped; check the tablet's sleep state yourself.\r\n");
        } else if (stopping) {
            SetWindowTextW(status, L"Stopped. Display settings and TightVNC remain available.");
            append_output(L"This launcher's Python processes stopped.\r\n");
        } else {
            _snwprintf(result, 200, L"\r\nPython exited with code %lu.\r\n", (unsigned long)wparam);
            result[199] = 0; append_output(result);
            SetWindowTextW(status, wparam ? L"Setup stopped. Read the error below, then retry." :
                           lparam == MODE_DIAGNOSE ? L"Diagnostics complete." : L"Session ended. You can start again.");
        }
        stopping = FALSE; set_controls(FALSE);
        return 0;
    }
    case WM_CLOSE:
        if (control_running) {
            close_after_control = TRUE;
            SetWindowTextW(status, L"Finishing the tablet command before closing...");
            append_output(L"Close requested: waiting for the tablet command to finish.\r\n");
            return 0;
        }
        if (job) { CloseHandle(job); job = NULL; }
        if (control_job) { CloseHandle(control_job); control_job = NULL; }
        DestroyWindow(target); return 0;
    case WM_DESTROY:
        if (log_font) DeleteObject(log_font);
        PostQuitMessage(0); return 0;
    }
    return DefWindowProcW(target, message, wparam, lparam);
}

int WINAPI wWinMain(HINSTANCE app, HINSTANCE previous, PWSTR command_line, int show) {
    (void)previous; (void)command_line;
    instance = app;
    DWORD length = GetModuleFileNameW(NULL, directory, PATH_CAP);
    if (!length || length >= PATH_CAP - 1) return 1;
    wchar_t *slash = wcsrchr(directory, L'\\');
    if (!slash) return 1;
    *slash = 0;
    file_path(script_path, L"start_remarkable_monitor.py");
    file_path(freeze_path, L"remarkable_sleep.py");
    file_path(log_path, L"launcher.log");
    HANDLE mutex = CreateMutexW(NULL, TRUE, L"Local\\RemarkableMonitorLauncher");
    if (!mutex) return 1;
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND existing = NULL;
        for (int attempt = 0; attempt < 20 && !existing; ++attempt) {
            existing = FindWindowW(CLASS_NAME, NULL);
            if (!existing) Sleep(50);
        }
        if (existing) {
            ShowWindow(existing, SW_RESTORE); SetForegroundWindow(existing);
            PostMessageW(existing, WM_RELAUNCH, 0, 0);
        }
        CloseHandle(mutex); return 0;
    }
    wchar_t old_log[PATH_CAP];
    file_path(old_log, L"launcher.previous.log");
    MoveFileExW(log_path, old_log, MOVEFILE_REPLACE_EXISTING);
    log_file = CreateFileW(log_path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    WNDCLASSEXW cls = {0};
    cls.cbSize = sizeof(cls); cls.lpfnWndProc = window_proc; cls.hInstance = app;
    cls.lpszClassName = CLASS_NAME; cls.hCursor = LoadCursorW(NULL, IDC_ARROW);
    cls.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    cls.hIcon = cls.hIconSm = LoadIconW(app, MAKEINTRESOURCEW(101));
    if (!RegisterClassExW(&cls)) { CloseHandle(mutex); return 1; }
    HWND handle = CreateWindowExW(0, CLASS_NAME, L"reMarkable Monitor", WS_OVERLAPPEDWINDOW,
                                  CW_USEDEFAULT, CW_USEDEFAULT, 820, 580,
                                  NULL, NULL, app, NULL);
    if (!handle) { CloseHandle(mutex); return 1; }
    ShowWindow(handle, show); UpdateWindow(handle);
    append_output(L"reMarkable Monitor launcher 1.5 / startup 1.4.0\r\nSettings: monitor_config.json. The tablet VDD adapter is auto-detected unless selected there.\r\nStop requests tablet sleep. Wake and AppLoad navigation are manual.\r\nSSH keys are retained across restarts and app updates.\r\n");
    if (log_file == INVALID_HANDLE_VALUE) append_output(L"The launcher log could not be saved. Check folder write access.\r\n");
    PostMessageW(handle, WM_AUTOSTART, 0, 0);
    MSG event;
    while (GetMessageW(&event, NULL, 0, 0) > 0) {
        if (!IsDialogMessageW(handle, &event)) { TranslateMessage(&event); DispatchMessageW(&event); }
    }
    if (job) CloseHandle(job);
    if (control_job) CloseHandle(control_job);
    if (log_file != INVALID_HANDLE_VALUE) CloseHandle(log_file);
    ReleaseMutex(mutex); CloseHandle(mutex);
    return 0;
}
