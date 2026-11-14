# -*- coding: utf-8 -*-
"""ldwin.py —— 雷电模拟器窗口定位 / 相对坐标 / 鼠标或 ADB 操作

目标：脚本里不再出现"写死的屏幕像素坐标"。

三层解耦：
  1. 窗口位置  —— 按窗口类名 LDPlayerMainFrame 自动查找，窗口随便拖、多开也能选实例；
  2. 画面区域  —— 自动识别安卓画面（扣掉顶部"雷电模拟器/微信"标签栏和右侧工具栏），
                  并把 DPI 缩放（125%/150%）统一到物理像素；
  3. 内部坐标  —— 一律用相对比例 (rx, ry) ∈ [0,1] 表示，0.5 就是画面正中间；
                  取图也用比例裁剪，窗口改大小、换分辨率都不用改脚本。

两条输入/取图通道，启动时自动选：
  * 鼠标通道：SetCursorPos + mouse_event，坐标是屏幕物理像素；
  * ADB 通道 ：模拟器自带 adb.exe 的 `exec-out screencap` + `input tap/swipe`。
    雷电如果以"管理员身份运行"，普通权限的脚本注入鼠标会被 Windows 的 UIPI 拒绝
    （SetCursorPos 抛 WinError 5），这时自动改走 ADB —— 不经过鼠标、不抢焦点，
    窗口被挡住甚至最小化也能跑。

依赖：Pillow、pytesseract(仅 OCR 用到)，其余全部标准库（不需要 pyautogui / pynput）。
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import ctypes.wintypes as wt
import io
import os
import subprocess
import threading
import time
from collections import namedtuple

from PIL import ImageStat

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
TARGET_CLASSES = ("LDPlayerMainFrame", "LDPlayer", "LdPlayerMainFrame")
TITLE_KEYWORDS = ("雷电模拟器", "LDPlayer", "雷电")
# 浏览器标题里也可能出现"雷电模拟器"（比如搜过这几个字），要排除掉
IGNORED_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass", "ApplicationFrameWindow")

DARK_MEAN = 75         # 灰度低于它算"暗"（雷电工具栏/标签栏约 31~43）
DARK_FRACTION = 0.75   # 一整列里暗像素超过这个比例，才认为是雷电的工具栏
SIDE_MAX_RATIO = 0.20  # 右侧工具栏最多占 20% 宽
# 雷电默认竖屏 9:16，也兼容 3:4 / 1:1 / 横屏，用来校验识别出来的画面区域
EXPECTED_RATIOS = ((9, 16), (3, 4), (1, 1), (4, 3), (16, 9))
RATIO_TOLERANCE = 0.06

# 有 adb 通道时优先用它：不抢焦点、窗口被遮挡也能跑、不受权限限制。
# 如果你的模拟器 ADB 不好用，改成 False 就退回鼠标通道。
PREFER_ADB = True

SW_RESTORE = 9
CREATE_NO_WINDOW = 0x08000000

Box = namedtuple("Box", "x y w h")          # 相对客户区左上角的像素框
Point = namedtuple("Point", "x y")          # 屏幕物理像素坐标

# --------------------------------------------------------------------------
# Win32 声明
# --------------------------------------------------------------------------
user32.GetClientRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
user32.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
user32.ClientToScreen.argtypes = (wt.HWND, ctypes.POINTER(wt.POINT))
user32.GetWindowTextLengthW.argtypes = (wt.HWND,)
user32.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
user32.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.mouse_event.argtypes = (wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t)
user32.GetWindowThreadProcessId.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
kernel32.GetCurrentProcessId.restype = wt.DWORD
kernel32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                ctypes.POINTER(wt.DWORD))
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
advapi32.GetTokenInformation.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wt.DWORD, ctypes.POINTER(wt.DWORD))

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

_DONE_DPI = False


def enable_dpi_awareness():
    """让本进程按物理像素取坐标/截图；不设的话 125% 缩放下坐标会被系统偷偷换算。

    必须在任何窗口/GDI 调用之前执行（模块导入时自动调用一次）。
    """
    global _DONE_DPI
    if _DONE_DPI:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    _DONE_DPI = True


enable_dpi_awareness()


# --------------------------------------------------------------------------
# 窗口枚举
# --------------------------------------------------------------------------
def _window_info(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return buf.value, cls.value


def list_windows():
    """所有可见顶层窗口：[(hwnd, 标题, 类名)]"""
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title, cls = _window_info(hwnd)
            if title or cls in TARGET_CLASSES:
                out.append((hwnd, title, cls))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def find_windows(title_keyword=None):
    """找所有雷电实例：优先按窗口类名，其次按标题关键字（排除浏览器）。"""
    hits = []
    for hwnd, title, cls in list_windows():
        if cls in TARGET_CLASSES:
            hits.append((hwnd, title, cls))
        elif cls not in IGNORED_CLASSES and (title_keyword or "雷电") in title:
            hits.append((hwnd, title, cls))
    return hits


# --------------------------------------------------------------------------
# 鼠标底层
# --------------------------------------------------------------------------
def _mouse(down=True):
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _cursor(pos):
    if not user32.SetCursorPos(int(pos[0]), int(pos[1])):
        raise ctypes.WinError(ctypes.get_last_error())


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return Point(pt.x, pt.y)


GA_ROOT = 2
user32.WindowFromPoint.argtypes = (wt.POINT,)
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = (wt.HWND, ctypes.c_uint)
user32.GetAncestor.restype = wt.HWND


def owner_of(screen_pos):
    """屏幕坐标上是哪个顶层窗口（用来判断目标位置有没有被别的窗口挡住）。"""
    hwnd = user32.WindowFromPoint(wt.POINT(int(screen_pos[0]), int(screen_pos[1])))
    if not hwnd:
        return None
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


# --------------------------------------------------------------------------
# 权限（完整性级别）—— 决定鼠标控制能不能用
# --------------------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
_IL_NAMES = {0x1000: "低(Low)", 0x2000: "普通(Medium)", 0x2100: "普通+",
             0x3000: "管理员(High)", 0x4000: "系统(System)"}

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wt.DWORD)
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)


def integrity_level(pid):
    """进程的完整性级别（0x1000 低 / 0x2000 普通 / 0x3000 管理员），失败返回 None。

    Windows 的 UIPI 规定：比前台窗口低的进程不能注入鼠标/键盘，
    SetCursorPos 会直接抛 WinError 5（拒绝访问）。
    """
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wt.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        size = wt.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0,
                                     ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 64))
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size,
                                            ctypes.byref(size)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        count = advapi32.GetSidSubAuthorityCount(sid)[0]
        return advapi32.GetSidSubAuthority(sid, count - 1)[0]
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def level_name(level):
    return _IL_NAMES.get(level, "未知(0x%X)" % level) if level is not None else "读不到"


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_path(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


# --------------------------------------------------------------------------
# ADB 通道：不经过鼠标，绕过 UIPI / 抢焦点 / 窗口遮挡
# --------------------------------------------------------------------------
ADB_TAP_MAX_MS = 4000
ADB_OCR_MAX_HEIGHT = 1440     # ADB 截屏 1080x1920，缩小一点再做 OCR 快些


class _PersistentShell:
    """常驻的 `adb shell -T sh`，点击/滑动/截屏不再每次新起 adb 进程。

    截屏用 `screencap -p | base64 -w 0` 传回：`adb shell` 会把输出里的
    \\n 翻成 \\r\\n，直接传 PNG 会坏；base64 里没有 0x0A，去掉空白再解码即可。
    """

    def __init__(self, adb_path, serial=None):
        self._token = ("__ADB_%d__" % int(time.time() * 1000)).encode("ascii")
        cmd = [adb_path]
        if serial:
            cmd += ["-s", serial]
        cmd += ["shell", "-T", "sh"]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=-1,
            creationflags=CREATE_NO_WINDOW)
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        fd = self._proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 20)
            except OSError:
                chunk = b""
            if not chunk:
                with self._cond:
                    self._eof = True
                    self._cond.notify_all()
                return
            with self._cond:
                self._buf += chunk
                self._cond.notify_all()

    def alive(self):
        return self._proc.poll() is None and not self._eof

    def command(self, cmd, timeout=15):
        """发一条命令，等到 token 出现；返回命令本身的输出（不含 token）。"""
        if not self.alive():
            raise RuntimeError("adb shell 已退出")
        try:
            self._proc.stdin.write(cmd + b"; echo " + self._token + b"\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise RuntimeError("adb shell 写入失败: %s" % exc)
        deadline = time.time() + timeout
        with self._cond:
            while True:
                idx = self._buf.find(self._token)
                if idx >= 0:
                    out = bytes(self._buf[:idx])
                    del self._buf[:idx + len(self._token)]
                    if self._buf.startswith(b"\r\n"):
                        del self._buf[:2]
                    elif self._buf.startswith(b"\n"):
                        del self._buf[:1]
                    return out
                if self._eof:
                    raise RuntimeError("adb shell 输出中断")
                left = deadline - time.time()
                if left <= 0:
                    raise RuntimeError("adb shell 命令超时")
                self._cond.wait(left)

    def close(self):
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write(b"exit\n")
                self._proc.stdin.flush()
                time.sleep(0.1)
        except Exception:
            pass
        try:
            self._proc.kill()
        except Exception:
            pass


class AdbChannel:
    """用模拟器自带的 adb.exe 截屏 + input tap/swipe，完全绕开 Windows 鼠标。"""

    def __init__(self, adb_path, serial=None):
        self.adb_path = adb_path
        self.serial = serial
        self.size = None            # 当前画面像素尺寸（screencap 给的，随旋转变化）
        self._shell = None
        self._shell_lock = threading.Lock()
        atexit.register(self.close)

    def _shell_cmd(self, cmd, timeout=15):
        """走常驻 shell；失败就丢掉 shell 并抛给上层，由上层退回单次 adb。"""
        with self._shell_lock:
            if self._shell is None or not self._shell.alive():
                if self._shell is not None:
                    self._shell.close()
                self._shell = _PersistentShell(self.adb_path, self.serial)
            try:
                return self._shell.command(cmd, timeout)
            except Exception:
                self._shell.close()
                self._shell = None
                raise

    def close(self):
        with self._shell_lock:
            if self._shell is not None:
                self._shell.close()
                self._shell = None

    def run(self, args, timeout=15, binary=False):
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        proc = subprocess.run(cmd + list(args), capture_output=True,
                              timeout=timeout, creationflags=CREATE_NO_WINDOW)
        if proc.returncode != 0:
            raise RuntimeError("adb %s 失败: %s"
                               % (" ".join(args),
                                  proc.stderr.decode("utf-8", "ignore").strip()))
        return proc.stdout if binary else proc.stdout.decode("utf-8", "ignore")

    def ensure_device(self):
        out = self.run(["devices"])
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return parts[0]
        return None

    def screenshot(self, max_height=ADB_OCR_MAX_HEIGHT):
        from PIL import Image
        try:
            b64 = self._shell_cmd(b"screencap -p | base64 -w 0", timeout=20)
            png = base64.b64decode(b"".join(b64.split()))
            img = Image.open(io.BytesIO(png))
            img.load()
        except Exception:
            raw = self.run(["exec-out", "screencap", "-p"], binary=True)
            img = Image.open(io.BytesIO(raw))
            img.load()
        self.size = img.size
        img = img.convert("RGB")
        if max_height and img.height > max_height:
            scale = max_height / img.height
            img = img.resize((max(1, int(img.width * scale)), max_height),
                             Image.LANCZOS)
        return img

    def tap(self, rx, ry):
        w, h = self.size or (1080, 1920)
        x, y = str(int(round(rx * w))), str(int(round(ry * h)))
        try:
            self._shell_cmd(("input tap %s %s" % (x, y)).encode("ascii"))
        except Exception:
            self.run(["shell", "input", "tap", x, y], timeout=15)

    def swipe(self, start, delta, duration=0.25):
        w, h = self.size or (1080, 1920)
        x1, y1 = int(round(start[0] * w)), int(round(start[1] * h))
        x2 = int(round((start[0] + delta[0]) * w))
        y2 = int(round((start[1] + delta[1]) * h))
        ms = int(max(50, duration * 1000))
        cmd = ("input swipe %d %d %d %d %d" % (x1, y1, x2, y2, ms)).encode("ascii")
        try:
            self._shell_cmd(cmd, timeout=15)
        except Exception:
            self.run(["shell", "input", "swipe", str(x1), str(y1), str(x2),
                      str(y2), str(ms)], timeout=15)


def find_adb(exe_hint=None):
    """找 adb.exe：优先用随程序带的 依赖\\adb\\adb.exe，其次模拟器目录、PATH。"""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "deps", "adb", "adb.exe"),
                  os.path.join(here, "deps", "platform-tools", "adb.exe")]
    if exe_hint:
        candidates.append(os.path.join(os.path.dirname(exe_hint), "adb.exe"))
    env = os.environ.get("LDPLAYER_ADB")
    if env:
        candidates.append(env)
    for root in (r"F:\leidian\LDPlayer14", r"C:\leidian\LDPlayer14",
                 r"D:\leidian\LDPlayer14", r"C:\LDPlayer\LDPlayer9"):
        candidates.append(os.path.join(root, "adb.exe"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which("adb")


def adb_ready(adb_path, serial=None):
    """探测 ADB 通道是否可用（模拟器设置里没开 ADB 调试就不可用）。"""
    if not adb_path:
        return False
    try:
        chan = AdbChannel(adb_path, serial)
        return bool(chan.ensure_device())
    except Exception:
        return False



# --------------------------------------------------------------------------
# 画面区域识别
# --------------------------------------------------------------------------
def _dark_fraction(gray, box, threshold=DARK_MEAN):
    """一列/一行里暗像素占多少（雷电工具栏是纯深灰，图标只占很小一块）。"""
    hist = gray.crop(box).histogram()
    total = sum(hist) or 1
    return sum(hist[:int(threshold)]) / total


def detect_content_box(img, expected_ratios=EXPECTED_RATIOS):
    """从客户区截图里算出安卓画面区域 (x, y, w, h)。

    做法：从右边缘往左走，把"绝大部分是深灰"的像素列当成雷电右侧工具栏；
    剩下的宽度按 9:16 反推画面高度，高度对不上就退回整个客户区。
    """
    w, h = img.size
    gray = img.convert("L")

    right = w
    max_side = max(1, int(w * SIDE_MAX_RATIO))
    while w - right < max_side and right > 1:
        if _dark_fraction(gray, (right - 1, 0, right, h)) > DARK_FRACTION:
            right -= 1
        else:
            break
    content_w = right

    # 用宽高比反推高度（雷电会把安卓画面等比缩放，高宽比基本固定）
    best_err, best_h = 9e9, h
    for rw, rh in expected_ratios:
        ch = content_w * rh / rw
        err = abs(h - ch) / h
        if err < best_err:
            best_err, best_h = err, ch

    if best_err > RATIO_TOLERANCE:
        return Box(0, 0, w, h)          # 识别不可信，就当整块都是画面

    content_h = min(int(round(best_h)), h)
    top = h - content_h
    return Box(0, top, content_w, content_h)


# --------------------------------------------------------------------------
# 窗口对象
# --------------------------------------------------------------------------
class EmulatorWindow:
    def __init__(self, hwnd, title="", class_name="", mode="auto"):
        self.hwnd = hwnd
        self.title = title
        self.class_name = class_name
        self._box = None
        self._box_key = None
        self._mode_pref = mode        # auto / mouse / adb
        self.mode = None              # 最终选定的通道
        self.mode_reason = ""
        self.channel = None           # AdbChannel
        self._adb_size = (0, 0)

    # ---------- 基础几何 ----------
    def exists(self):
        return bool(user32.IsWindow(self.hwnd))

    def assert_alive(self):
        if not self.exists():
            raise RuntimeError("雷电模拟器窗口已关闭（hwnd=%s），请重新启动模拟器" % self.hwnd)

    @property
    def window_rect(self):
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return Box(r.left, r.top, r.right - r.left, r.bottom - r.top)

    @property
    def client_origin(self):
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return Point(pt.x, pt.y)

    @property
    def client_size(self):
        r = wt.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return Box(0, 0, r.right - r.left, r.bottom - r.top)

    @property
    def minimized(self):
        return bool(user32.IsIconic(self.hwnd))

    # ---------- 画面区域 ----------
    def content_box(self, refresh=False):
        """安卓画面在客户区里的像素框，带缓存（窗口大小变了自动重算）。"""
        if self.mode == "adb":       # ADB 截屏本身就是整块安卓画面
            if self._adb_size == (0, 0):
                self.channel.screenshot()
                self._adb_size = self.channel.size or (1080, 1920)
            return Box(0, 0, self._adb_size[0], self._adb_size[1])
        self.assert_alive()
        size = self.client_size
        key = (size.w, size.h, bool(self.minimized))
        if refresh or self._box is None or self._box_key != key:
            img = self.grab_client()
            self._box = detect_content_box(img)
            self._box_key = key
        return self._box

    def grab_client(self):
        """截客户区（含标签栏、工具栏）。"""
        from PIL import ImageGrab
        self.assert_alive()
        org, size = self.client_origin, self.client_size
        if size.w <= 0 or size.h <= 0:
            raise RuntimeError("窗口被最小化了，取不到画面（请先 restore）")
        if self._covered():          # 挡住时截到的是别的窗口，必须先修好再干活
            self.activate()
            time.sleep(0.05)
            if self._covered():
                raise RuntimeError(
                    "雷电窗口被其他窗口挡住了，截图/识别会取到别的窗口，已停止。\n"
                    "请把模拟器切到最前面再运行。")
        return ImageGrab.grab(bbox=(org.x, org.y, org.x + size.w, org.y + size.h),
                              all_screens=True)

    def grab_content(self):
        """只截安卓画面（推荐 OCR / 找按钮都用这个）。"""
        if self.resolve_mode() == "adb":
            img = self.channel.screenshot()
            self._adb_size = img.size
            return img
        try:
            box = self.content_box()
            img = self.grab_client()
        except RuntimeError as exc:          # 最小化 / 被遮挡 -> 走 ADB 截屏
            if self._to_adb("%s" % exc):
                shot = self.channel.screenshot()
                self._adb_size = shot.size
                return shot
            raise
        return img.crop((box.x, box.y, box.x + box.w, box.y + box.h))

    # ---------- 比例 <-> 屏幕坐标 ----------
    def to_screen(self, rx, ry):
        box, org = self.content_box(), self.client_origin
        return Point(int(round(org.x + box.x + rx * box.w)),
                     int(round(org.y + box.y + ry * box.h)))

    def to_ratio(self, screen_pos):
        """屏幕坐标 -> 画面相对比例（校准用）。"""
        box, org = self.content_box(), self.client_origin
        return ((screen_pos[0] - org.x - box.x) / box.w,
                (screen_pos[1] - org.y - box.y) / box.h)

    def region(self, rx, ry, rw, rh):
        """比例矩形 -> 屏幕像素框 (left, top, w, h)，可直接喂给 ImageGrab/OCR。"""
        box, org = self.content_box(), self.client_origin
        left = org.x + box.x + rx * box.w
        top = org.y + box.y + ry * box.h
        return (int(round(left)), int(round(top)),
                max(1, int(round(rw * box.w))), max(1, int(round(rh * box.h))))

    # ---------- 动作 ----------
    def activate(self):
        """还原 + 提到前台，避免点到别的窗口上（走 ADB 时不需要，也不抢焦点）。"""
        if self.mode == "adb":
            return True
        self.assert_alive()
        if self.minimized:
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(0.2)
        if user32.GetForegroundWindow() == self.hwnd:
            return True
        target = user32.GetWindowThreadProcessId(self.hwnd, None)
        me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(me, target, True)
        try:
            user32.BringWindowToTop(self.hwnd)
            user32.SetForegroundWindow(self.hwnd)
        finally:
            user32.AttachThreadInput(me, target, False)
        time.sleep(0.15)
        return user32.GetForegroundWindow() == self.hwnd

    def _covered(self):
        """判断窗口有没有被别的窗口挡住：在客户区撒 5x3 个点看归属，命中率 <80% 算挡住。

        只取 3 个点会漏判"右边被压住一半"的情况，截图就成了别家的画面，
        OCR 会把别的窗口文字当成题目，所以这里宁可判严一点（判严只会多走 ADB 通道）。
        """
        org, size = self.client_origin, self.client_size
        if size.w <= 0 or size.h <= 0:
            return False
        probes = []
        for rx in (0.02, 0.25, 0.5, 0.75, 0.98):
            for ry in (0.05, 0.5, 0.95):
                probes.append((org.x + int(size.w * rx), org.y + int(size.h * ry)))
        hits = sum(1 for p in probes if owner_of(p) == self.hwnd)
        return hits < len(probes) * 0.8

    def ensure_front(self, pos=None):
        """确保窗口在前台、且目标点确实落在模拟器上，否则报错（绝不盲点）。"""
        self.activate()
        if pos is not None:
            if owner_of(pos) != self.hwnd:
                time.sleep(0.2)
                self.activate()
            if owner_of(pos) != self.hwnd:
                raise RuntimeError(
                    "目标点 %s 被其他窗口挡住了，为了不乱点已经停下。\n"
                    "请把雷电窗口切到最前面（或先手动点一下模拟器）再运行。" % (pos,))
        elif self._covered():
            raise RuntimeError("雷电窗口被其他窗口挡住了，请先切到最前面再运行。")
        return True

    def move(self, rx, ry):
        pos = self.to_screen(rx, ry)
        _cursor(pos)
        return pos

    # ---------- 通道选择 ----------
    def resolve_mode(self, prefer=None):
        """决定用鼠标还是 ADB：屏幕上点不动（权限不够）就自动走 ADB。"""
        if prefer:
            self._mode_pref = prefer
        if self.mode:
            return self.mode

        adb_path = find_adb(process_path(window_pid(self.hwnd)))
        if adb_path and (self._mode_pref == "adb" or adb_ready(adb_path)):
            self.channel = AdbChannel(adb_path)
        else:
            self.channel = None

        if self._mode_pref == "mouse":
            self.mode = "mouse"
        elif self._mode_pref == "adb":
            if not self.channel:
                raise RuntimeError("指定了 ADB 通道，但连不上模拟器（先把雷电的 ADB 调试打开）")
            self.mode = "adb"
        elif self.channel and PREFER_ADB:
            self.mode = "adb"
            mine = integrity_level(os.getpid())
            emu = integrity_level(window_pid(self.hwnd))
            if mine is not None and emu is not None and mine < emu:
                self.mode_reason = ("雷电以 %s 运行、脚本只有 %s，系统会拒绝鼠标操作，"
                                    "已走 ADB 通道" % (level_name(emu), level_name(mine)))
            else:
                self.mode_reason = "ADB 不抢焦点、窗口被挡住也能用，比鼠标稳"
        elif self.channel:
            mine = integrity_level(os.getpid())
            emu = integrity_level(window_pid(self.hwnd))
            if mine is not None and emu is not None and mine < emu:
                # 模拟器以管理员运行 -> 普通权限注入鼠标会被 UIPI 拒绝(WinError 5)
                self.mode = "adb"
                self.mode_reason = ("雷电以 %s 运行，本脚本只有 %s，"
                                    "系统会拒绝鼠标操作，已改用 ADB 通道"
                                    % (level_name(emu), level_name(mine)))
            else:
                self.mode = "mouse"
        else:
            self.mode = "mouse"
            if self._mode_pref == "auto":
                self.mode_reason = "没找到可用的 adb.exe，只能用鼠标通道（需要窗口在前台且不被遮挡）"
        return self.mode

    def _to_adb(self, reason=""):
        try:
            if not self.channel and self.mode != "adb":
                adb_path = find_adb(process_path(window_pid(self.hwnd)))
                if adb_path and adb_ready(adb_path):
                    self.channel = AdbChannel(adb_path)
            if not self.channel:
                return False
            if self._adb_size == (0, 0):
                self.channel.screenshot()
                self._adb_size = self.channel.size or (1080, 1920)
        except Exception:
            return False
        self.mode = "adb"
        if reason:
            print("  ⚠ %s" % reason)
            print("  → 已切换到 ADB 通道（adb 截屏 + input tap），不经过鼠标、不受权限限制")
        return True

    def describe(self):
        mode = self.resolve_mode()
        if mode == "adb":
            w, h = self._adb_size
            if w:
                return "通道: ADB（%s）画面 %dx%d" % (
                    os.path.basename(self.channel.adb_path), w, h)
            return "通道: ADB（%s）" % os.path.basename(self.channel.adb_path)
        return "通道: 鼠标（窗口 %dx%d）" % (self.client_size.w, self.client_size.h)

    def _blocked_hint(self, exc):
        emu = level_name(integrity_level(window_pid(self.hwnd)))
        mine = level_name(integrity_level(os.getpid()))
        return ("鼠标操作被系统拒绝：%s\n"
                "  雷电以 %s 运行，脚本只有 %s，Windows 的 UIPI 不允许低权限进程控制它。\n"
                "  解决办法（任选其一）：\n"
                "   ① 让雷电的 ADB 调试可用（雷电设置 -> 其他设置 -> 打开 ADB 调试），"
                "脚本会自动走 ADB 通道；\n"
                "   ② 右键“以管理员身份运行”再执行本脚本；\n"
                "   ③ 取消雷电的“以管理员身份运行”（快捷方式属性 -> 兼容性），重启雷电。"
                % (exc, emu, mine))

    # ---------- 动作 ----------
    def click(self, rx, ry, settle=0.08):
        if self.resolve_mode() == "adb":
            self.channel.tap(rx, ry)
            time.sleep(0.08)
            return None
        try:
            pos = self.to_screen(rx, ry)
            self.ensure_front(pos)
            _cursor(pos)
            time.sleep(0.05)
            _mouse(True)
            time.sleep(settle)
            _mouse(False)
        except (OSError, RuntimeError) as exc:   # WinError 5 = 被 UIPI 拒绝
            if self._to_adb("鼠标操作不可用（%s）" % exc):
                self.channel.tap(rx, ry)
                time.sleep(0.08)
                return None
            raise RuntimeError(self._blocked_hint(exc)) from exc
        return pos

    def double_click(self, rx, ry, interval=0.08):
        if self.resolve_mode() == "adb":
            self.channel.tap(rx, ry)
            time.sleep(interval)
            self.channel.tap(rx, ry)
            return
        pos = self.to_screen(rx, ry)
        self.ensure_front(pos)
        _cursor(pos)
        for _ in range(2):
            _mouse(True)
            time.sleep(0.03)
            _mouse(False)
            time.sleep(interval)

    def swipe(self, start, delta, duration=0.25, steps=12):
        """滑动/拖拽：start 是起点比例，delta 是相对画面宽高的位移比例。"""
        if self.resolve_mode() == "adb":
            self.channel.swipe(start, delta, duration)
            return None, None
        try:
            p0 = self.to_screen(*start)
            self.ensure_front(p0)
        except (OSError, RuntimeError) as exc:
            if self._to_adb("鼠标操作不可用（%s）" % exc):
                self.channel.swipe(start, delta, duration)
                return None, None
            raise RuntimeError(self._blocked_hint(exc)) from exc
        dx, dy = delta
        p1 = self.to_screen(start[0] + dx, start[1] + dy)
        try:
            _cursor(p0)
            time.sleep(0.03)
            _mouse(True)
            step = max(1, steps)
            for i in range(1, step + 1):
                _cursor((p0.x + (p1.x - p0.x) * i / step,
                         p0.y + (p1.y - p0.y) * i / step))
                time.sleep(duration / step)
            _mouse(False)
        except OSError as exc:
            try:
                _mouse(False)
            except Exception:
                pass
            if self._to_adb("鼠标操作被系统拒绝（%s）" % exc):
                self.channel.swipe(start, delta, duration)
                return None, None
            raise RuntimeError(self._blocked_hint(exc)) from exc
        time.sleep(0.15)
        return p0, p1

    def __repr__(self):
        return "<EmulatorWindow hwnd=%s %r>" % (self.hwnd, self.title)


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------
_default = None


def get_window(index=0, title_keyword=None, refresh=False, mode="auto"):
    """拿到（并缓存）雷电窗口；找不到就抛错并给出排查提示。"""
    global _default
    if _default is not None and not refresh and _default.exists():
        return _default
    hits = find_windows(title_keyword)
    if not hits:
        raise RuntimeError(
            "没找到雷电模拟器窗口。请确认：\n"
            "  1) 雷电已经启动到安卓桌面；\n"
            "  2) 窗口没有最小化；\n"
            "  3) 如果改过其他皮肤/版本，可用 python ldwin.py --列表 查看类名后再改 TARGET_CLASSES。")
    hits.sort(key=lambda h: (h[2] not in TARGET_CLASSES, -_area(h[0])))
    hwnd, title, cls = hits[min(index, len(hits) - 1)]
    _default = EmulatorWindow(hwnd, title, cls, mode=mode)
    return _default


def _area(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.right - r.left) * (r.bottom - r.top)


def click(rx, ry, **kw):
    return get_window().click(rx, ry, **kw)


def screenshot():
    return get_window().grab_content()


def wait(seconds):
    time.sleep(seconds)


# --------------------------------------------------------------------------
# 诊断 / 校准：python ldwin.py
# --------------------------------------------------------------------------
def annotate(win, points=(), path="画面标注.png"):
    """把识别到的画面区域画上 0.1 网格，并标出给定的比例点，方便肉眼核对。"""
    from PIL import ImageDraw
    img = win.grab_content().convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for i in range(1, 10):
        x, y = int(w * i / 10), int(h * i / 10)
        draw.line([(x, 0), (x, h)], fill=(255, 0, 0), width=1)
        draw.line([(0, y), (w, y)], fill=(255, 0, 0), width=1)
        draw.text((x + 3, 3), "%.1f" % (i / 10), fill=(255, 0, 0))
        draw.text((3, y + 1), "%.1f" % (i / 10), fill=(255, 0, 0))
    for rx, ry, label in points:
        cx, cy = int(rx * w), int(ry * h)
        draw.line([(cx - 18, cy), (cx + 18, cy)], fill=(0, 0, 255), width=3)
        draw.line([(cx, cy - 18), (cx, cy + 18)], fill=(0, 0, 255), width=3)
        draw.text((cx + 22, cy - 8), "%s (%.3f, %.3f)" % (label, rx, ry),
                  fill=(0, 0, 255))
    img.save(path)
    return path


def _main():
    import sys
    args = sys.argv[1:]
    if "--列表" in args:
        print("可见窗口：")
        for hwnd, title, cls in list_windows():
            mark = "  <-- 雷电" if cls in TARGET_CLASSES else ""
            print("  hwnd=%-8s class=%-30s title=%r%s" % (hwnd, cls, title, mark))
        return
    try:
        win = get_window()
    except Exception as exc:
        print(exc)
        return

    pid = window_pid(win.hwnd)
    print("窗口: %r  hwnd=%s  类名=%s" % (win.title, win.hwnd, win.class_name))
    print("进程: %s (pid=%s)" % (process_path(pid), pid))
    print("权限: 脚本=%s  雷电=%s" % (level_name(integrity_level(os.getpid())),
                                      level_name(integrity_level(pid))))
    adb_path = find_adb(process_path(pid))
    print("ADB : %s  %s" % (adb_path or "未找到",
                            "可用" if adb_path and adb_ready(adb_path) else "不可用"))
    mode = win.resolve_mode()
    print("通道: %s" % mode + ("（%s）" % win.mode_reason if win.mode_reason else ""))
    if mode == "mouse":
        print("客户区: %dx%d @ (%d, %d)" % (win.client_size.w, win.client_size.h,
                                            win.client_origin.x, win.client_origin.y))
    box = win.content_box(refresh=True) or None
    if mode == "mouse":
        print("安卓画面: %dx%d  客户区内偏移 (%d, %d)  宽高比 %.4f"
              % (box.w, box.h, box.x, box.y, box.h / box.w if box.w else 0))
        for name, (rx, ry) in (("画面中心", (0.5, 0.5)), ("左上", (0.0, 0.0)),
                               ("右下", (1.0, 1.0))):
            print("  %s (%.2f, %.2f) -> 屏幕 %s" % (name, rx, ry, win.to_screen(rx, ry)))
    path = annotate(win)
    print("已保存标注图: %s （红网格=0.1 等分，方便核对坐标）" % path)


if __name__ == "__main__":
    _main()
