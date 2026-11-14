# -*- coding: utf-8 -*-
"""卡包.py —— 单词卡包自动答题（识别驱动，不依赖窗口大小/位置，离线判题）

怎么做到"不依赖窗口"：
  * 取图走 ADB（adb exec-out screencap），拿到的是安卓画面本身，跟窗口多大、
    窗口在哪、有没有被别的窗口挡住统统无关；
  * 选项靠文字结构认：行首是 A./B./C./D. 且带中文 => 就是选项，
    位置、间距、卡片大小变了都不影响，点的是这一行文字框的中心；
  * 题干靠结构认：选项上方、字母最多的那段英文；
  * 屏幕类型靠结构认：有选项=题目，没选项=单词卡，各自处理；
  * 标准版判题全离线：RapidOCR 读字 + 离线词表判题，全程不联网、不用 Ollama；
    加 --模型 可启用看图模型（需要 Ollama）。

流程（和你的用法一致）：每轮 5 个单词卡 + 5 道题，上下滑动切换，共 5 轮。

用法：
    python 卡包.py             # 5 轮，每轮 10 步
    python 卡包.py 50          # 只按步数跑 50 步
    python 卡包.py --走查 20    # 只识别不点击，逐步告诉你认出了什么（安全试跑）
    python 卡包.py --调试       # 只看当前这一屏
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass

from PIL import Image

import ldict
import ldwin
import rapid_ocr


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
# 判题方式：llm_first        = 本地 LLM 优先判题（默认；错题本仍最优先，LLM 挂了退回词表）
#           offline          = 只用本地词表（不联网）
#           offline_then_llm = 词表判不出来 / 没把握时才问本地 LLM
#           api              = 用百度翻译接口（需要联网 + key）
#           offline_then_api = 词表查不到时再用 API
MATCH_MODE = "llm_first"

# 本地 Ollama 模型。没开 / 模型没拉下来时自动退回词表判题，不会卡流程。
LLM_BASE = "http://127.0.0.1:11434"

# 看图用的模型（Ollama）。两种角色，按模型名里有没有 "ocr" 自动定：
#   read  = 只读字：把截图转录成文字（题干/单词 + 四个选项），判题交给词表/文本模型
#   judge = 直接判题：看截图后自己选出正确选项
# 实测：
#   glm-ocr:q8_0    读字 0.3~0.5s/屏，中文选项认得最准（read，默认）
#   qwen3.5:9b      判题 6/6，预热后约 2~5s/屏（judge）
#   bonsai-2:2b     约 0.2s，但判题 0/6、读题也容易跑偏
#   qwen3.8:latest  27B，本机加载会 500（内存不够）
# 换模型只改这一行；也可运行时 `python 卡包.py --模型 glm-ocr:q8_0` 临时指定。
VISION_MODEL = "glm-ocr:q8_0"
VISION_ROLE = "read"       # read=只读字 / judge=看截图判题（--模型 会按模型名自动改）
LLM_MODEL = "qwen3.5:9b"   # 文本兜底判题用它（read 模式下词表没把握时由它判）
LLM_TIMEOUT = 20          # 秒；含首次加载模型的时间，给足一点
LLM_ASK_BELOW = 0.20      # offline_then_llm 模式用：词表最高分低于这个也去问一次 LLM

# 视觉模型：把截图直接交给它读题/单词并判题，OCR 与词表退为兜底。
# 标准版默认关闭（RapidOCR + 离线词表已实测整包全对，不用 Ollama）；
# 想启用：python 卡包.py --模型 glm-ocr:q8_0（需要 Ollama 在跑且已拉模型）
USE_VISION = False
VISION_TIMEOUT = 60          # 秒；首次调用要加载 9B 模型，给足
VISION_MAX_EDGE = 1280       # 送模型前按最长边缩放，越小越快
VISION_READ_OPTIONS = True   # 连四个选项文字一起读（更稳但慢些；False 只读题干+答案）
VISION_PROMPT_FULL = (
    "这是一张英语学习 App 的截图。如果是选择题，读出题干和四个选项并选出正确选项；"
    "如果是单词卡，只读出卡片上的单词。只输出一行 JSON，不要解释："
    '{"type":"question","question":"题干","options":["A选项","B选项","C选项","D选项"],"answer":"B"}')
VISION_PROMPT_FAST = (
    "这是一张英语学习 App 的截图。如果是选择题，读出题干并选出正确选项；"
    "如果是单词卡，只读出卡片上的单词。只输出一行 JSON，不要解释："
    '{"type":"question","question":"题干","answer":"B"}')
# read 角色（OCR 模型）用：只让它转录，不给任何 JSON 模板
# —— glm-ocr 会把模板原样抄回来，但它逐行读字又快又准
VISION_PROMPT_LINES = "逐行输出图中所有文字，一行一条，不要解释，不要输出 JSON。"

# 百度翻译的 key 不写死在代码里；要用 API 判题时，
# 设环境变量 BAIDU_APPID / BAIDU_SECRET_KEY 即可（离线模式完全用不到）。
APPID = os.environ.get("BAIDU_APPID", "")
SECRET_KEY = os.environ.get("BAIDU_SECRET_KEY", "")
TRANSLATE_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
MAX_RETRIES = 3
TIMEOUT = 5

# OCR 引擎：RapidOCR（PP-OCRv5 mobile / ONNX，纯 CPU，整屏热态约 0.3s）。
# 模型和依赖随包在 deps 里，不联网；标准版只带这一个引擎，
# 启动时会检查文件齐不齐，缺了直接提示退出、不碰模拟器。
OCR_ENGINE = "rapid"

ROUNDS = 5                 # 外循环轮数
STEPS_PER_ROUND = 10       # 每轮走几步（5 个单词卡 + 5 道题）
SWIPE_START = (0.5, 0.66)  # 滑动起点（画面比例，画面=安卓画面）
SWIPE_DELTA = (0.0, -0.24)  # 向上滑
AFTER_CONFIRM_WAIT = 1.0   # 点完弹窗"确认"后等多久（等弹窗淡出，不然遮罩会毁掉 OCR）
STUCK_LIMIT = 3            # 连续几次画面没变就认为滑动失效，停下
UNKNOWN_LIMIT = 3          # 连续几次认不出界面就干净退出，不乱滑
TAIL_STEPS = 40            # 主步数用完后的收尾上限（点「继续」过强化轮用）
# 实在判断不出来时：stop=停下（默认，不乱蒙） / first=总选A / random=随便蒙一个
UNSURE_ACTION = "stop"

# 答完顺手读对错高亮：答错时把青色（正确）选项记进错题本。
# 复用点完题那一帧，不额外截图、不增加等待，读不到就静默跳过。
LEARN_FROM_HIGHLIGHT = True

# 选项四行的位置学过之后，后面的题目只 OCR "题干 + 选项"区域，不再整屏识别。
# 页面上出现宽青色按钮（结算/完成/弹窗）或区域里认不出 4 个选项时，自动退回整屏 OCR。
FAST_OPTION_OCR = True

OPTION_RE = re.compile(r"^\s*([A-Da-d])\s*[.．、,:：)）]\s*|^\s*([A-Da-d])\s+|^\s*([A-Da-d])(?=[\u4e00-\u9fff])")
# 读字模型的选项行：带标点的 "A." 或 "A 油" 这种"字母+空格+文字"，
# 只用来判断"这屏有选项"，宁严勿松（误伤只会退回整屏 OCR）
VISION_OPTION_RE = re.compile(r"^([A-Da-d])\s*[.、．:：)）]|^([A-Da-d])\s+\S")
PROGRESS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

_translate_cache = {}
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "已见单词.json")
SEEN_WORDS = []          # 单词卡上见过的词，题干认糊时用来对（会存盘，跨次运行也能用）


def _load_seen():
    try:
        import json
        with open(SEEN_FILE, encoding="utf-8") as f:
            words = json.load(f)
        if isinstance(words, list):
            SEEN_WORDS.extend(str(w) for w in words if isinstance(w, str))
    except Exception:
        pass


def _save_seen():
    try:
        import json
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(SEEN_WORDS[-400:], f, ensure_ascii=False)
    except Exception:
        pass


def remember_word(word):
    word = (word or "").strip()
    if not word or " " in word or word in SEEN_WORDS:
        return
    if not ldict.get_dict().form(word.lower()):
        return          # 词表里查不到的多半是 OCR 噪声（"alr"/"WOYrse"），记了只会误导
    SEEN_WORDS.append(word)
    _save_seen()


_load_seen()


# --------------------------------------------------------------------------
# 错题本：答错的题，界面上青色那行就是正确答案，记下来下次直接用
# （强化轮反复遇到的就是这些词；词表认不认得出无所谓，记住就行。答错会覆盖更新）
# --------------------------------------------------------------------------
WRONGBOOK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "错题.json")
WRONGBOOK = {}


def _load_wrongbook():
    try:
        import json
        with open(WRONGBOOK_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    WRONGBOOK[k.strip().lower()] = v
    except Exception:
        pass


def _save_wrongbook():
    """把错题本写回磁盘；写失败只提示，不影响答题。"""
    try:
        with open(WRONGBOOK_FILE, "w", encoding="utf-8") as f:
            json.dump(WRONGBOOK, f, ensure_ascii=False, indent=1)
        return True
    except Exception as exc:
        print("  （错题本写入失败：%s）" % exc)
        return False


def _norm_opt(text):
    """选项文字的归一化：去空格和标点（OCR 的空格/标点不稳定，字才是关键）。"""
    return re.sub(r"[\s\.。,，;；、:：!！?？（()）\[\]【】\"'’]+", "", text or "")


def wrongbook_lookup(key):
    """查错题本：精确 -> 去空格 -> 近邻（题干认糊时，"aIr"也能对回"alr"）。"""
    if not WRONGBOOK or not key:
        return None
    key = key.strip().lower()
    if key in WRONGBOOK:
        return WRONGBOOK[key]
    packed = key.replace(" ", "")
    if packed in WRONGBOOK:
        return WRONGBOOK[packed]
    if len(key) >= 3:
        match = difflib.get_close_matches(key, list(WRONGBOOK), n=1, cutoff=0.6)
        if match:
            return WRONGBOOK[match[0]]
    return None


def match_option(answer_text, options):
    """把记住的答案文字对回当前屏的选项，返回下标；对不上返回 None。"""
    want = _norm_opt(answer_text)
    if not want:
        return None
    best, best_score = None, 0.0
    for i, opt in enumerate(options):
        text = _norm_opt(opt.text)
        if not text:
            continue
        if text == want:
            return i
        score = difflib.SequenceMatcher(None, want, text).ratio()
        if len(want) >= 2 and (want in text or text in want):
            score = max(score, 0.9)      # 单字符太容易误配（"n"撞上"n大门"），不参与
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.6 else None


_load_wrongbook()


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------
def _cjk_count(text):
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _cjk_ratio(text):
    return _cjk_count(text) / len(text) if text else 0.0


def _latin_ratio(text):
    if not text:
        return 0.0
    n = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return n / len(text)


def preprocess_text(text):
    text = re.sub(r"[^\w\s]", "", text)
    return text.lower().strip()


def extract_keywords(text):
    return Counter(text.split())


def similarity(a, b):
    """等价于原来的 Levenshtein.ratio，用标准库实现，少一个第三方依赖。"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def keyword_match_score(k1, k2):
    common = sum(min(k1[k], k2[k]) for k in set(k1) & set(k2))
    total = sum(k1.values()) + sum(k2.values())
    return 2 * common / total if total else 0.0


# --------------------------------------------------------------------------
# 翻译（只有 MATCH_MODE 用得到 API 时才会走到这里，离线模式完全不联网）
# --------------------------------------------------------------------------
def translate(text):
    if not APPID or not SECRET_KEY:
        print("  没配百度翻译 key（环境变量 BAIDU_APPID / BAIDU_SECRET_KEY），用不了 API 判题")
        return ""
    import requests        # 延迟导入：离线判题时不需要这个库
    key = text.strip()
    if not key:
        return ""
    if key in _translate_cache:
        return _translate_cache[key]

    salt = str(random.randint(32768, 65536))
    sign = hashlib.md5((APPID + key + salt + SECRET_KEY).encode()).hexdigest()
    params = {"q": key, "from": "en", "to": "zh", "appid": APPID,
              "salt": salt, "sign": sign}
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(TRANSLATE_URL, params=params, timeout=TIMEOUT)
            result = resp.json()
            if "trans_result" in result:
                dst = " ".join(item["dst"] for item in result["trans_result"])
                _translate_cache[key] = dst
                return dst
            print("  翻译失败: %s" % result.get("error_msg", "未知错误"))
        except Exception as exc:
            print("  翻译异常(第%d次): %s" % (attempt + 1, exc))
        time.sleep(1)
    return ""


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
@dataclass
class TextLine:
    text: str
    left: int
    top: int
    width: int
    height: int
    letter: str = ""        # 选项行才有：A/B/C/D（万一认漏了选项，字母也不会错位）
    raw: str = ""           # 精修前的原始文字，精修反而认糊了还能用它兜底

    @property
    def right(self):
        return self.left + self.width

    @property
    def bottom(self):
        return self.top + self.height

    @property
    def cx(self):
        return self.left + self.width / 2

    @property
    def cy(self):
        return self.top + self.height / 2

    def __str__(self):
        return "%-34r [x=%d y=%d w=%d h=%d]" % (self.text, self.left, self.top,
                                                self.width, self.height)


def _join(prev, cur):
    if not prev:
        return cur
    if prev[-1].isascii() and prev[-1].isalnum() and cur[0].isascii() and cur[0].isalnum():
        return prev + " " + cur
    return prev + cur


def _rapid_lines(img, box=None):
    """RapidOCR 识别成 TextLine 行（已按上下、左右排序）；调用失败返回 None。"""
    rows = rapid_ocr.recognize(img, box)
    if rows is None:
        return None
    lines = [TextLine(t, left, top, right - left, bottom - top)
             for (t, left, top, right, bottom) in rows]
    lines.sort(key=lambda l: (l.top, l.left))
    return lines


def ocr_lines(img):
    """整屏 OCR：RapidOCR 识别成行。"""
    lines = _rapid_lines(img)
    if lines is None:
        raise RuntimeError("RapidOCR 调用失败（标准版未带 Tesseract 后备）")
    return lines


def ocr_words(img):
    """词级 OCR（不合并成行）——用来找"确认"按钮这种小目标。"""
    lines = _rapid_lines(img)
    if lines is None:
        raise RuntimeError("RapidOCR 调用失败（标准版未带 Tesseract 后备）")
    return lines


# --------------------------------------------------------------------------
# 认屏幕结构（这一步决定"不依赖位置"）
# --------------------------------------------------------------------------
def find_options(lines):
    """行首是 A./B./C./D. 且带中文 => 选项。返回按 A、B、C、D 排好的列表。"""
    found = {}
    for line in lines:
        match = OPTION_RE.match(line.text)
        if not match:
            continue
        letter = (match.group(1) or match.group(2) or match.group(3)).upper()
        text = line.text[match.end():].strip(" .、")
        if letter in found or not text:
            continue
        # 选项基本是中文；偶尔字被认糊了（比如 "adv.IME"）也别丢，否则字母会错位
        if _cjk_count(text) < 1 and len(text) < 3:
            continue
        found[letter] = TextLine(text, line.left, line.top, line.width, line.height,
                                 letter)
    ordered = [found[k] for k in "ABCD" if k in found]
    return ordered if len(ordered) >= 2 else []


def pick_question(lines, options):
    """题干 = 选项上方那组英文（可能换行，取字母最多的一连续段）。音标行跳过。"""
    limit = options[0].top if options else 10 ** 9
    cands = [l for l in lines
             if _latin_ratio(l.text) >= 0.6
             and any(ch.isascii() and ch.isalpha() for ch in l.text)
             and not l.text.lstrip().startswith("[")      # [ˈeəpleɪn] 这种音标
             and not re.search(r"[\[\]ˈˌːəɪʊæɑɒɔɜθðʃʒŋ]", l.text)
             and not OPTION_RE.match(l.text)              # "A. xxx" 是选项
             and l.bottom <= limit]
    if not cands:
        return ""
    cands.sort(key=lambda l: l.top)
    groups = [[cands[0]]]
    for line in cands[1:]:
        prev = groups[-1][-1]
        if line.top - prev.bottom <= max(prev.height, line.height) * 0.8:
            groups[-1].append(line)
        else:
            groups.append([line])
    best = max(groups, key=lambda g: sum(len(x.text) for x in g))
    return " ".join(x.text for x in best).strip()


def screen_progress(lines):
    """顺手读一下 "学习1/25" 这种进度，日志里好对账。"""
    for line in lines:
        m = PROGRESS_RE.search(line.text)
        if m and _cjk_ratio(line.text) < 0.5:
            done, total = int(m.group(1)), int(m.group(2))
            if 0 < total <= 999:
                return done, total
    return None


# --------------------------------------------------------------------------
# 局部精修：整屏那一遍只管找结构；文字裁出来放大再认一遍才准
# （整屏认 "toe" 会整块漏掉、认 "v.敲,击" 会丢字，裁出来放大就正常）
# --------------------------------------------------------------------------
def _squeeze_cjk(text):
    """去掉中文字之间的空格："脚 跃" -> "脚跃"。"""
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+([，。；：、,.!?])", r"\1", text)
    return text.strip()


def _ocr_crop(img, box, scale=2):
    """裁出小区域放大交给 RapidOCR 认（小字放大后认得更准）。"""
    left, top, right, bottom = (max(0, int(box[0])), max(0, int(box[1])),
                                min(img.width, int(box[2])),
                                min(img.height, int(box[3])))
    if right - left < 8 or bottom - top < 8:
        return ""
    region = img.crop((left, top, right, bottom)).convert("L")
    region = region.resize((region.width * scale, region.height * scale), Image.LANCZOS)
    rows = rapid_ocr.recognize(region)
    if rows is None:
        return ""
    return "\n".join(row[0] for row in rows)


def _ocr_lines_on(img, box):
    """在指定区域做 OCR，返回行。"""
    lines = _rapid_lines(img, box)
    if lines is None:
        raise RuntimeError("RapidOCR 调用失败（标准版未带 Tesseract 后备）")
    return lines


def refine_options(img, options):
    """把选项那块放大重认，按"纵向位置"把新文字套回原来的框。

    还顺带补漏：整屏那遍偶尔会漏掉一个选项（比如 "C." 没认出来），
    放大那遍往往能认全，就把缺的那个补上。
    """
    if not options:
        return options
    # 把裁剪区往上多放一行：最上面那个选项的 "A." 标记丢了时，那一行也要框进来
    if len(options) >= 2:
        gaps = [options[i + 1].cy - options[i].cy for i in range(len(options) - 1)]
        spacing = sum(gaps) / len(gaps)
    else:
        spacing = options[0].height * 2 or 80
    box = (min(o.left for o in options) - 80,
           options[0].top - max(18, spacing * 1.2),
           max(o.right for o in options) + 240,
           options[-1].bottom + 28)
    lines = _ocr_lines_on(img, box)
    used = set()
    out = []
    for opt in options:
        pick = None
        for i, line in enumerate(lines):
            if i in used:
                continue
            if abs(line.cy - opt.cy) > max(opt.height, line.height) * 0.8:
                continue
            if pick is None or lines[pick].width < line.width:
                pick = i
        if pick is None:
            out.append(opt)
            continue
        used.add(pick)
        text = lines[pick].text
        match = OPTION_RE.match(text)
        if match:
            text = text[match.end():]
        text = _squeeze_cjk(text.strip(" .、"))
        out.append(TextLine(text, opt.left, opt.top, opt.width, opt.height,
                            opt.letter, opt.text) if text else opt)

    known = {o.letter for o in out}
    for i, line in enumerate(lines):
        if i in used:
            continue
        match = OPTION_RE.match(line.text)
        if not match:
            continue
        letter = (match.group(1) or match.group(2) or match.group(3)).upper()
        if letter in known:
            continue
        text = _squeeze_cjk(line.text[match.end():].strip(" .、"))
        if not text:
            continue
        known.add(letter)
        out.append(TextLine(text, line.left, line.top, line.width, line.height,
                            letter))

    # 补漏 2：连字母都没认出来（"A."丢了），但那一行中文还在 —— 按位置补
    missing = [c for c in "ABCD" if c not in known]
    if len(missing) == 1 and len(out) >= 2:
        out.sort(key=lambda o: o.top)
        gaps = [out[i + 1].cy - out[i].cy for i in range(len(out) - 1)]
        spacing = sum(gaps) / len(gaps) if gaps else 90
        low, high = out[0].cy - spacing * 1.4, out[-1].cy + spacing * 1.4
        for i, line in enumerate(lines):
            if i in used:
                continue
            if _cjk_count(line.text) < 1:
                continue
            if not (low <= line.cy <= high):
                continue
            if any(abs(line.cy - o.cy) < spacing * 0.5 for o in out):
                continue                       # 挨着已有选项，是同一行被拆开，不是新选项
            text = _squeeze_cjk(OPTION_RE.sub("", line.text).strip(" .、"))
            if text:
                out.append(TextLine(text, line.left, line.top, line.width,
                                    line.height, missing[0]))
                known.add(missing[0])
                break

    out.sort(key=lambda o: o.top)
    return out


def question_candidates(img, options, lines):
    """题干多认几遍：不同 psm 各给一个结果，后面对不上字典时还能换一个试试。

    一旦某个候选本身是"认识的单词"（在词表里、或刚在卡片上见过），
    剩下的 psm 和放大兜底就不用跑了 —— 那几遍是给认糊了的时候准备的。
    整句题不满足"单个词"这个形状，照旧全跑。
    """
    if options:
        bottom = options[0].top - max(36, int(options[0].height * 1.5))
    else:
        bottom = img.height * 0.62
    box = (img.width * 0.28, img.height * 0.14, img.width * 0.72, bottom)
    from_lines = pick_question(lines, options)
    dictionary = ldict.get_dict()
    seen = {w.lower() for w in SEEN_WORDS}

    def has_confident():
        for text in found + ([from_lines] if from_lines else []):
            word = text.strip().lower().rstrip(".")
            if re.fullmatch(r"[a-z][a-z'\-]{1,40}", word) \
                    and (word in seen or dictionary.form(word)):
                return True
        return False

    found = []
    if not has_confident():
        for line in _ocr_crop(img, box).splitlines():
            line = line.strip()
            if not line or not re.fullmatch(r"[A-Za-z][A-Za-z'’\-\. ]{0,70}", line):
                continue
            if OPTION_RE.match(line):
                continue
            if line not in found:
                found.append(line)
    if from_lines and from_lines not in found:
        found.append(from_lines)

    # 兜底：把题干那一行单独裁出来放大 3 倍、只允许字母，专治"tra slate"这种粘字漏字
    anchor = next((l for l in lines if l.text == from_lines), None)
    if anchor is None and from_lines:
        anchor = next((l for l in lines
                       if _latin_ratio(l.text) >= 0.6 and l.text in from_lines), None)
    if anchor is not None and not has_confident():
        tight = (anchor.left - 14, anchor.top - 14,
                 anchor.right + 14, anchor.bottom + 14)
        word = _ocr_crop(img, tight, scale=3).strip()
        word = word.splitlines()[0].strip() if word.strip() else ""
        if word and re.fullmatch(r"[A-Za-z][A-Za-z'’\-]{1,40}", word) and word not in found:
            found.append(word)
    return found


def pick_best_question(candidates):
    """候选里优先挑"这一轮刚在卡片上见过"的词，其次挑字典认识的，再其次看长度。

    光看"字典里有没有"不够：音标行也可能被认成一个合法单词（woman 的音标被认成 aeons），
    而题目考的必然是刚看过的那几张卡上的词。
    """
    if not candidates:
        return ""
    dictionary = ldict.get_dict()
    seen = {w.lower() for w in SEEN_WORDS}
    best, best_score = "", -1
    for text in candidates:
        tokens = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
                  if len(t) >= 2]
        if not tokens:
            continue
        known = sum(1 for t in tokens
                    if t not in ldict.STOPWORDS and dictionary.form(t))
        score = known * 10 + len(text)
        if text.lower() in seen:
            score += 100
        if score > best_score:
            best, best_score = text, score
    return best or max(candidates, key=len)


def refine_word(img):
    """单词卡中间的词，同样裁出来放大认（整屏经常整块漏掉）。"""
    box = (img.width * 0.28, img.height * 0.26, img.width * 0.72, img.height * 0.56)
    best = ""
    for line in _ocr_crop(img, box).splitlines():
        line = line.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z'’\- ]{0,60}", line) and len(line) > len(best):
            best = line
    return best


# --------------------------------------------------------------------------
# 弹窗（"必须要作答哦~" —— 在答题页上滑就会弹出来，不点掉什么都干不了）
# --------------------------------------------------------------------------
DIALOG_HINTS = ("作答",)      # 只认"必须要作答哦~"，光"必须"会被选项文字误伤
CONFIRM_WORDS = ("确认", "确定", "知道了", "好的", "继续")


def _find_confirm_button(img):
    """找"确认"按钮的框：先认字，认不出来就用颜色找那条青色按钮。"""
    words = ocr_words(img)
    for word in words:
        if word.text in CONFIRM_WORDS:
            return word
    for first in words:
        for second in words:
            if first is second or second.left < first.left:
                continue
            height = max(first.height, second.height)
            if abs(first.cy - second.cy) > height * 0.8:      # 不在同一行
                continue
            if second.left - first.right > 3 * height:        # 隔太远
                continue
            joined = first.text + second.text
            if joined in CONFIRM_WORDS:
                return TextLine(joined, first.left, min(first.top, second.top),
                                second.right - first.left,
                                max(first.bottom, second.bottom)
                                - min(first.top, second.top))
    return None


def _is_teal(pixel):
    """这个应用的主色青绿：答题高亮是 (96,199,186)，弹窗按钮是 (32,211,189)。"""
    r, g, b = pixel
    return g > 150 and b > 140 and r < 150 and abs(g - b) < 50


def _hilite_band(img, hint=None):
    """弹窗里那条青色按钮：白字压青底，OCR 常常整条认不出来，直接用颜色找。"""
    top = (hint.bottom + 4) if hint is not None else int(img.height * 0.45)
    left, right = int(img.width * 0.30), int(img.width * 0.70)
    bottom = min(img.height, top + int(img.height * 0.35))
    region = img.crop((left, top, right, bottom))
    if region.width < 20 or region.height < 10:
        return None
    small = region.convert("RGB").resize((80, 120))
    raw = small.tobytes()
    width = small.width

    def teal_at(x, y):
        i = (y * width + x) * 3
        return _is_teal((raw[i], raw[i + 1], raw[i + 2]))

    rows = []
    for y in range(small.height):
        hit = sum(1 for x in range(width) if teal_at(x, y))
        rows.append(hit >= width * 0.25)

    best_start, best_len, start = None, 0, None
    for y, ok in enumerate(rows + [False]):
        if ok and start is None:
            start = y
        elif not ok and start is not None:
            if y - start > best_len:
                best_len, best_start = y - start, start
            start = None
    if best_start is None or best_len < 3:
        return None

    ys = range(best_start, best_start + best_len)
    xs = [x for x in range(width) if any(teal_at(x, y) for y in ys)]
    if not xs:
        return None
    scale_y = region.height / small.height
    scale_x = region.width / small.width
    box_left = left + min(xs) * scale_x
    box_right = left + (max(xs) + 1) * scale_x
    box_top = top + best_start * scale_y
    box_bottom = top + (best_start + best_len) * scale_y
    return TextLine("确认", int(box_left), int(box_top),
                    int(box_right - box_left), int(box_bottom - box_top))


def find_teal_button(img, min_width_ratio=0.12, min_height_ratio=0.018):
    """找"又宽又实"的青绿色按钮条（确认/继续都用这个样式，白字压青底认不出来）。

    卡片的青色边框只有几像素宽，缩小后基本消失，不会误判。
    """
    small = img.convert("RGB").resize((200, max(20, int(200 * img.height / img.width))))
    raw = small.tobytes()
    width, height = small.size

    def teal_at(x, y):
        i = (y * width + x) * 3
        return _is_teal((raw[i], raw[i + 1], raw[i + 2]))

    rows, spans = [], []
    for y in range(height):
        hits = [x for x in range(width) if teal_at(x, y)]
        if not hits:
            rows.append(False)
            spans.append(None)
            continue
        span = (hits[0], hits[-1] + 1)
        # 按钮上有白色文字，"青色连续段"会被切断，所以按"这一行青色的总量"判断
        ok = len(hits) >= width * min_width_ratio and span[1] - span[0] >= width * min_width_ratio
        rows.append(ok)
        spans.append(span if ok else None)

    min_h = max(3, int(min_height_ratio * height))
    best_band = None
    start = None
    for y, ok in enumerate(rows + [False]):
        if ok and start is None:
            start = y
        elif not ok and start is not None:
            if y - start >= min_h:
                seg = spans[start:y]
                left = min(s[0] for s in seg if s)
                right = max(s[1] for s in seg if s)
                if best_band is None or (right - left) > (best_band[1] - best_band[0]):
                    best_band = (left, right, start, y)
            start = None
    if best_band is None:
        return None
    sx, sy = img.width / width, img.height / height
    left, right, top, bottom = best_band
    return TextLine("按钮", int(left * sx), int(top * sy),
                    int((right - left) * sx), int((bottom - top) * sy))


def detect_dialog(img, lines):
    """有"必须要作答"这类提示 => 返回 (提示行, 确认按钮框)；否则 None。"""
    hint = next((l for l in lines
                 if any(h in l.text for h in DIALOG_HINTS)
                 and not OPTION_RE.match(l.text)), None)      # 选项文字里的"必须"不算
    if hint is None:
        return None
    return hint, (_find_confirm_button(img) or _hilite_band(img, hint)
                  or find_teal_button(img))


TUTORIAL_HINTS = ("点击页面", "单词释义", "下一个单词", "上滑")


def _tutorial_hint(lines):
    """新手引导的提示文字（点击翻面 / 上滑下一个）。"""
    for line in lines:
        if any(h in line.text for h in TUTORIAL_HINTS):
            return line.text.strip()
    return ""


def _tutorial_dark(img):
    """新手引导的深色遮罩：只看中间应用区域，避开左右白边。"""
    w, h = img.size
    region = img.convert("L").crop((int(w * 0.35), int(h * 0.15),
                                    int(w * 0.65), int(h * 0.85)))
    if region.width < 8 or region.height < 8:
        return False
    hist = region.resize((32, 32)).histogram()
    mean = sum(i * c for i, c in enumerate(hist)) / float(32 * 32)
    return mean < 110


def is_pack_list(lines):
    """卡包列表页（"选择卡包" + "初级词汇1-10" 这种分类）—— 跑到这里说明一包结束了。"""
    text = " ".join(l.text for l in lines)
    return "选择卡包" in text


def is_final_page(lines):
    """整个包彻底跑完的完成页（大字 COMPLETE、恭喜你完成卡包学习）。
    点完「完成」就直接收工 —— 跳到哪不用管，不一定回卡包列表。"""
    text = " ".join(l.text for l in lines).lower()
    return any(k in text for k in ("complete", "完成卡包学习", "本次学习", "恭喜"))


def is_summary(lines):
    """强化结算页（待强化单词 N、再来一遍）—— 点「继续」进强化轮。"""
    if is_final_page(lines):
        return False
    text = " ".join(l.text for l in lines).lower()
    return any(k in text for k in ("待强化", "再来一遍", "进入强化"))


def _image_changed(before, after, threshold=0.2):
    """两张截图有没有明显变化（判断点了之后界面到底跳没跳）。"""
    from PIL import ImageChops, ImageStat
    if before is None or after is None or before.size != after.size:
        return True
    diff = ImageChops.difference(before.convert("L"), after.convert("L"))
    return ImageStat.Stat(diff).mean[0] > threshold


# 答完题那一屏：正确项是青条(96,199,186)、选错项是红条(255,143,145)
HILITE_TEAL = (96, 199, 186)
HILITE_RED = (255, 143, 145)


def _near(pixel, target, tol=45):
    return all(abs(pixel[i] - target[i]) <= tol for i in range(3))


def _reinforce_tab_active(img):
    """顶部标签栏的「强化」是不是点亮状态（青色字）—— 跨运行判断在不在强化轮。

    学习轮时"强化"两个字是灰色，强化轮是青色；进度条在更上面，不在这个区域。
    """
    w, h = img.size
    region = img.crop((int(w * 0.53), int(h * 0.075), int(w * 0.60), int(h * 0.115)))
    raw = region.convert("RGB").resize((30, 8)).tobytes()
    hits = 0
    for i in range(0, len(raw), 3):
        if _is_teal((raw[i], raw[i + 1], raw[i + 2])):
            hits += 1
    return hits >= 5          # 实测：学习轮 0/240，强化轮 16/240


def looks_answered(img):
    """这一屏是不是"已经答过、正显示对错"？靠青条/红条的颜色判断。"""
    region = img.crop((int(img.width * 0.36), int(img.height * 0.45),
                       int(img.width * 0.54), int(img.height * 0.92)))
    if region.width < 4 or region.height < 4:
        return False
    raw = region.convert("RGB").resize((48, 96)).tobytes()
    total = len(raw) // 3
    hits = 0
    for i in range(0, len(raw), 3):
        px = (raw[i], raw[i + 1], raw[i + 2])
        if _near(px, HILITE_TEAL) or _near(px, HILITE_RED):
            hits += 1
    return hits / max(1, total) > 0.12


def _row_hilite(img, option):
    """统计某个选项那一行里青/红高亮的占比。

    选项中线上横着取一条窄带，把整行底色都算进来：答对是青条、答错是红条，
    文字像素占比很小，所以 0.25 的阈值足够和普通白底区分开。
    """
    w, h = img.size
    x0, x1 = int(w * 0.30), int(w * 0.70)
    y = int(option.cy)
    if not (0 <= y < h) or x1 <= x0:
        return 0.0, 0.0
    band = img.convert("RGB").crop((x0, max(0, y - 4), x1, min(h, y + 5)))
    raw = band.resize((180, 1)).tobytes()
    total = len(raw) // 3
    if not total:
        return 0.0, 0.0
    teal = red = 0
    for k in range(0, len(raw), 3):
        px = (raw[k], raw[k + 1], raw[k + 2])
        if _near(px, HILITE_TEAL, 40):
            teal += 1
        elif _near(px, HILITE_RED, 40):
            red += 1
    return teal / total, red / total


def learn_from_answer(img, options, question, chosen_index):
    """从"点完题"那一帧的对错高亮里学正确答案，答错时写进错题本。

    复用调用方已经拍好的 after 帧（不额外截图）。只认唯一一条青色高亮，
    读不到、或本来就答对了，就返回 None —— 宁可少记，不能记错。
    """
    if not LEARN_FROM_HIGHLIGHT or img is None or not options:
        return None
    key = (question or "").strip().lower()
    if not key:
        return None

    teal, red = [], []
    for i, opt in enumerate(options):
        t, r = _row_hilite(img, opt)
        if t >= 0.25:
            teal.append(i)
        if r >= 0.25:
            red.append(i)
    if len(teal) != 1:
        return None
    correct = teal[0]
    if correct == chosen_index:
        return None                      # 本来答对，没什么可学的
    if red and chosen_index not in red:
        return None                      # 高亮和实际点的行对不上，宁可不记

    text = (options[correct].text or "").strip()
    if not _norm_opt(text):
        return None
    old = WRONGBOOK.get(key)
    if old is not None and _norm_opt(old) == _norm_opt(text):
        return None                      # 已经是这个答案，不重复写盘
    WRONGBOOK[key] = text
    _save_wrongbook()
    return correct


# --------------------------------------------------------------------------
# 判题
# --------------------------------------------------------------------------
def _unsure_index(options):
    """实在判断不出来就随便蒙一个 —— 宁可错也别把流程卡死。

    返回 None 表示按配置要求停下（UNSURE_ACTION = "stop"）。
    """
    if not options or UNSURE_ACTION == "stop":
        return None
    if UNSURE_ACTION == "first":
        return 0
    return random.randrange(len(options))


def choose_by_pos(question, options):
    """字面对不上时的兜底：按词性选。

    "waiter" 字典里写"侍者"，选项写"服务员"，字面永远对不上；
    但四个选项里只有它是名词（其余是 v./adj./conj.），词性就能定下来。
    只有当"词性唯一"时才返回，唯一性不成立就宁可停下不乱选。
    """
    gloss = ldict.get_dict().form((question or "").strip().lower())
    pos = ldict.part_of_speech(gloss)
    if not pos:
        return None
    hits = [i for i, o in enumerate(options)
            if ldict.part_of_speech(o.text) == pos]
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# 本地视觉模型 / 文本 LLM（Ollama）
#   视觉：看真实截图读题+判题（首选）
#   文本：Vision 不可用、或只拿到 OCR 文字时的兜底
# --------------------------------------------------------------------------
_model_state = {}   # 模型名 -> 在不在；每个模型只探测一次


def _model_present(model):
    """探测 Ollama 在不在、这个模型拉没拉；结果记住，本次运行不再反复试。"""
    if model not in _model_state:
        try:
            import urllib.request
            with urllib.request.urlopen(LLM_BASE + "/api/tags", timeout=3) as resp:
                models = json.loads(resp.read().decode("utf-8")).get("models", [])
            _model_state[model] = any(m.get("name") == model or
                                      m.get("model") == model for m in models)
        except Exception:
            _model_state[model] = False
        if not _model_state[model]:
            print("（本地模型不可用：Ollama 没开或还没拉 %s）" % model)
    return _model_state[model]


def llm_available():
    """文本判题模型在不在；不在就退回纯词表判题。"""
    return _model_present(LLM_MODEL)


def _pick_letter(text):
    """从回复里抠出选项字母。模型多半只回一个字母，但也可能夹解释文字。"""
    text = (text or "").strip()
    if not text:
        return None
    match = re.search(r"(?:答案|answer)\s*[是为]?\s*[:：]?\s*\(?([A-Da-d])\b",
                      text, re.I)
    if match:
        return match.group(1).upper()
    if re.fullmatch(r"\(?[A-Da-d]\s*[.、．)）]?", text):
        return text[0].upper()
    letters = re.findall(r"\b([A-Da-d])\b", text)
    if letters:
        return letters[-1].upper()    # 解释文字里最后提到的多半是结论
    return None


def llm_choose(question, options):
    """问本地 LLM 选哪个选项；返回 (下标, 得分, 判题方式) 或 None。"""
    if not llm_available() or not question or not options:
        return None
    lines = ["下面是一道英语选择题，选出正确选项。只回答一个字母（A、B、C 或 D），不要解释。",
             "", "题目：%s" % question, ""]
    for i, opt in enumerate(options[:4]):
        lines.append("%s. %s" % (opt.letter or "ABCD"[i], opt.text or opt.raw))
    lines += ["", "答案："]
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": "\n".join(lines)}],
        "stream": False,
        "think": False,       # 模型默认开思维链，会把这几十个 token 全用在思考上
        "keep_alive": "10m",
        "options": {"temperature": 0, "num_predict": 64},
    }
    try:
        import urllib.request
        req = urllib.request.Request(LLM_BASE + "/api/chat",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("（本地 LLM 调用失败：%s）" % exc)
        return None
    content = ((reply.get("message") or {}).get("content") or "")
    letter = _pick_letter(content)
    if not letter:
        print("（本地 LLM 没给出字母，回复是：%r）" % content.strip()[:60])
        return None
    index = "ABCD".index(letter)
    if index >= len(options):
        return None
    return index, 0.6, "本地LLM"


# --------------------------------------------------------------------------
# 视觉识别 + 判题：整张截图交给本地视觉模型
# --------------------------------------------------------------------------
def _image_b64(img, max_edge=VISION_MAX_EDGE):
    """截图 -> base64 JPEG（先缩放压缩再送模型，越小越快）。"""
    im = img.convert("RGB")
    if max_edge:
        scale = max_edge / max(im.size)
        if scale < 1:
            im = im.resize((max(1, int(im.width * scale)),
                            max(1, int(im.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_vision(content):
    """解析视觉模型的回复（容忍 ```json 包裹、question/word 字段名混用）。"""
    text = re.sub(r"```[a-zA-Z]*", "", content or "").strip("` \n")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    word = str(obj.get("question") or obj.get("word") or obj.get("content")
               or obj.get("text") or obj.get("card") or "").strip()
    options = []
    raw_options = obj.get("options")
    if isinstance(raw_options, list):
        for item in raw_options:
            item = re.sub(r"^\s*[A-Da-d]\s*[.、．:：)）]\s*", "", str(item)).strip()
            if item:
                options.append(item)
    answer = str(obj.get("answer") or "").strip().upper()
    answer = answer[0] if answer[:1] in ("A", "B", "C", "D") else ""
    return {"word": word, "options": options, "answer": answer}


def _parse_vision_lines(content):
    """read 角色：模型只负责逐行转录，这里按行挑出题干/单词和 A-D 选项。

    返回 {"word":..., "options":[...], "answer":""}；选项不足四个就不给，
    调用方会保留 OCR 原来的选项文字（少一个都对不上位，宁可不换）。
    """
    text = re.sub(r"```[a-zA-Z]*", "", content or "")
    rows = [l.strip() for l in text.splitlines() if l.strip()]
    options = {}
    word = ""
    for row in rows:
        m = re.match(r"^([A-Da-d])\s*[.、．:：)）]\s*(.+)$", row)
        if m:
            options.setdefault(m.group(1).upper(), m.group(2).strip())
            continue
        if options or word:
            continue
        # 第一个带英文的行就是卡片单词或题干（音标括号去掉）
        if re.search(r"[A-Za-z]{2,}", row):
            word = re.sub(r"\s*\[[^\]]*\]\s*$", "", row).strip()
    letters = sorted(options)
    texts = [options[l] for l in letters]
    if len(texts) != 4 or letters != ["A", "B", "C", "D"]:
        texts = []
    return {"word": word, "options": texts, "answer": "", "lines": rows}


def _apply_vision_options(options, vision):
    """把读字模型读到的四个选项文字，按屏幕上到下顺序贴回 OCR 的选项行。

    只换文字、不换位置：行坐标和高亮核对仍用 OCR 的结果。
    """
    texts = (vision or {}).get("options") or []
    rows = sorted(options, key=lambda o: o.top)
    if len(texts) != len(rows) or not texts:
        return
    for row, text in zip(rows, texts):
        row.text = text


# 选项四行的相对位置（0~1）：这个 App 每屏的选项位置是固定的，第一次用 OCR
# 学到后就不必再整屏识别。页面高度/宽度变了会重学；进入强化轮也会重学。
_OPT_CACHE = {"boxes": None, "size": None}

# 单词卡背面读到的 app 释义：word -> 中文释义。判题时比词表更贴近出题口径。
_CARD_GLOSS = {}
CARD_TAP = (0.5, 0.66)     # 卡片中心（画面比例），点一下翻到背面

# 卡包节奏是"5 张单词卡 + 5 道题"。读完一张卡后，如果确定下一屏还是卡，
# 就在上滑动画中点一下，直接落在下一张的"单词+释义"帧，省掉一次正面识别。
CHAIN_CARD_TAP = True
CHAIN_TAP = (0.5, 0.22)    # 串联点击点：卡片上部；还要避开题目选项框
CHAIN_SWIPE_DURATION = 0.25 # 和普通上滑一致：手势本身别引入新变量
CHAIN_LAST_CARD = 3        # 只在前 3 张卡后串联：给"漏看一张卡"留一次余量


def _remember_option_boxes(img, options):
    if len(options) != 4:
        return
    w, h = img.size
    _OPT_CACHE["boxes"] = [(o.left / w, o.top / h, o.width / w, o.height / h)
                           for o in options]
    _OPT_CACHE["size"] = (w, h)


def forget_option_boxes():
    _OPT_CACHE["boxes"] = None


def _vision_text_lines(vision):
    """把读字模型的逐行文字包成 TextLine（没有坐标），给纯文字判断用：
    弹窗提示、结算/完成/卡包列表、进度 "学习12/25"。"""
    return [TextLine(text, 0, i * 40, 100, 30)
            for i, text in enumerate((vision or {}).get("lines") or [])]


def _rows_look_right(img, rows):
    """粗查缓存的四行位置还对不对：行里有字、行间是空白。

    只是护栏，判不准就返回 False 退回整屏 OCR —— 慢一点，但不会点错。
    """
    if len(rows) != 4:
        return False
    gray = img.convert("L")
    w, h = gray.size
    for row in rows:
        if row.left < 0 or row.top < 0 or row.right > w or row.bottom > h:
            return False
        if row.width < 40 or row.height < 16:
            return False
    for a, b in zip(rows, rows[1:]):
        if b.top - a.bottom < 4:
            return False
        gap = gray.crop((int(a.left), int(a.bottom), int(a.right), int(b.top)))
        total = gap.width * gap.height
        if total and sum(gap.histogram()[:110]) / float(total) > 0.12:
            return False
    for row in rows:
        crop = gray.crop((int(row.left), int(row.top),
                          int(row.right), int(row.bottom)))
        total = crop.width * crop.height
        if not total or sum(crop.histogram()[:110]) / float(total) < 0.008:
            return False
    return True


def _cached_option_rows(img, vision):
    """位置缓存 + 读字模型的文字 => 直接拼出四行；不可用返回 None。"""
    boxes = _OPT_CACHE["boxes"]
    texts = (vision or {}).get("options") or []
    if not boxes or len(texts) != 4 or _OPT_CACHE["size"] != img.size:
        return None
    w, h = img.size
    rows = [TextLine(text, int(l * w), int(t * h), int(bw * w), int(bh * h),
                     letter="ABCD"[i])
            for i, (text, (l, t, bw, bh)) in enumerate(zip(texts, boxes))]
    return rows if _rows_look_right(img, rows) else None


def vision_screen(img):
    """把整张截图交给看图模型：read 只读字，judge 读题+判题。不可用返回 None。"""
    if not USE_VISION or not _model_present(VISION_MODEL):
        return None
    if VISION_ROLE == "read":
        prompt = VISION_PROMPT_LINES
    else:
        prompt = VISION_PROMPT_FULL if VISION_READ_OPTIONS else VISION_PROMPT_FAST
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": prompt,
                      "images": [_image_b64(img)]}],
        "stream": False,
        "think": False,          # 默认开思维链，会吃光 token 导致回复为空
        "keep_alive": "10m",
        "options": {"temperature": 0, "num_predict": 300},
    }
    try:
        import urllib.request
        req = urllib.request.Request(LLM_BASE + "/api/chat",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("（视觉模型调用失败：%s）" % exc)
        return None
    content = ((reply.get("message") or {}).get("content") or "")
    if VISION_ROLE == "read":
        parsed = _parse_vision_lines(content)
        if not parsed.get("word") and not parsed.get("options"):
            print("（读字模型没读出内容，原回复：%r）" % content.strip()[:60])
            return None
        return parsed
    parsed = _parse_vision(content)
    if parsed is None:
        print("（视觉模型没返回 JSON，原回复：%r）" % content.strip()[:60])
    return parsed


def vision_pick(vision, options):
    """把视觉模型给的答案对回当前屏的选项下标（返回 options 里的下标）。"""
    if not vision or not options:
        return None
    answer = vision.get("answer") or ""
    if not answer:
        return None
    order = sorted(range(len(options)), key=lambda i: options[i].top)  # 自上而下=A,B,C,D
    rows = [options[i] for i in order]
    index = "ABCD".index(answer)
    # 优先按"正确选项的文字"对：OCR 把字母认错也不怕
    texts = vision.get("options") or []
    if index < len(texts) and texts[index]:
        hit = match_option(texts[index], rows)
        if hit is not None:
            return order[hit]
    # 退一步按字母位置对（四行都在时才敢这么点）
    if len(rows) == 4 and index < len(rows):
        return order[index]
    return None


def choose(question, options, vision=None):
    """返回 (选项下标, 得分, 判题方式)。

    顺序：单词卡背面释义（app 自己的标准答案）最优先，其次错题本。
    read 模式（glm-ocr 只读字）：背面 -> 错题本 -> 词表 -> 文本 LLM。
    judge 模式（qwen3.5 看截图）：背面 -> 视觉模型 -> 文本 LLM -> 词表。
    """
    texts = [o.text for o in options]
    raws = [o.raw or o.text for o in options]

    def from_wrongbook(reason):
        remembered = wrongbook_lookup(question)
        if remembered is None:
            return None
        idx = match_option(remembered, options)
        return (idx, 1.0, "错题本") if idx is not None else None

    def from_card_gloss():
        gloss = _CARD_GLOSS.get((question or "").strip().lower())
        if not gloss:
            return None
        # 优先用精修后的文字；只有精修完全对不上时才退回原始文字。
        # 以前对 text/raw 取 max，raw 一糊就可能把错误选项顶到 1.00。
        scores = ldict.rank_gloss(texts, gloss)
        if not scores or max(scores) <= 0:
            scores = ldict.rank_gloss(raws, gloss)
        if not scores:
            return None
        best = max(range(len(scores)), key=lambda i: scores[i])
        if scores[best] <= 0:
            return None
        return best, scores[best], "卡片背面"

    hit = from_card_gloss()
    if hit is not None:
        return hit

    if MATCH_MODE == "llm_first":
        # 错题本是记死的正确答案（从界面学回来的），比重新判一遍靠谱，仍放最前。
        hit = from_wrongbook("llm")
        if hit is not None:
            return hit
        scores = [max(a, b) for a, b in
                  zip(ldict.rank_options(texts, question),
                      ldict.rank_options(raws, question))]
        best = max(range(len(scores)), key=lambda i: scores[i]) if scores else 0
        best_score = scores[best] if scores else 0.0
        if VISION_ROLE == "read":
            # 选项文字是读字模型给的，只要词表能对上就直接用 —— 文本模型是 9B，
            # 和 OCR 模型塞不进同一块 8G 显存，来回换一次要重载十几秒，能不问就不问。
            if best_score > 0:
                return best, best_score, "离线词表"
            if USE_VISION:   # 标准版默认离线：只有显式 --模型 开了视觉才问文本模型
                asked = llm_choose(question, options)
                if asked is not None:
                    return asked
            return 0, 0.0, "离线词表(没查到词)"
        # judge 模式：视觉模型自己会选答案（上面已试），不行再问文本 LLM，最后退回词表
        picked = vision_pick(vision, options)
        if picked is not None:
            return picked, 0.7, "视觉模型"
        asked = llm_choose(question, options)
        if asked is not None:
            return asked
        if best_score > 0:
            return best, best_score, "离线词表(LLM不可用)"
        return 0, 0.0, "离线词表(没查到词)"

    if MATCH_MODE in ("offline", "offline_then_api", "offline_then_llm"):
        scores = [max(a, b) for a, b in
                  zip(ldict.rank_options(texts, question),
                      ldict.rank_options(raws, question))]
        best = max(range(len(scores)), key=lambda i: scores[i]) if scores else 0
        best_score = scores[best] if scores else 0.0
        if best_score > 0 and not (MATCH_MODE == "offline_then_llm"
                                   and best_score < LLM_ASK_BELOW):
            return best, best_score, "离线词表"
        if MATCH_MODE == "offline_then_llm":
            # 错题本是记死的答案，比谁都有优先权；其次问 LLM；
            # 都不行就还按词表那个低分答案走（score>0 时）。
            hit = from_wrongbook("offline")
            if hit is not None:
                return hit
            asked = llm_choose(question, options)
            if asked is not None:
                return asked
            if best_score > 0:
                return best, best_score, "离线词表(低置信)"
            return 0, 0.0, "离线词表(没查到词)"
        if MATCH_MODE == "offline":
            hit = from_wrongbook("offline")
            if hit is not None:
                return hit
            return 0, 0.0, "离线词表(没查到词)"
        hit = from_wrongbook("offline")
        if hit is not None:
            return hit

    chinese = translate(question)
    if not chinese:
        return 0, 0.0, "翻译失败"
    target = preprocess_text(chinese)
    t_kw = extract_keywords(target)

    def api_score(text):
        cand = preprocess_text(text)
        return (0.7 * similarity(target, cand)
                + 0.3 * keyword_match_score(t_kw, extract_keywords(cand)))

    scores = [max(api_score(a), api_score(b)) for a, b in zip(texts, raws)]
    if max(scores or [0]) > 0:
        best = max(range(len(scores)), key=lambda i: scores[i])
        return best, scores[best], "翻译API"
    hit = from_wrongbook("api")
    if hit is not None:
        return hit
    return 0, 0.0, "翻译失败"


# --------------------------------------------------------------------------
# 一屏：识别 -> 处理
# --------------------------------------------------------------------------
def snap_to_seen(question):
    """题干认糊了（"tra slate"）就用刚看过的单词卡去对——这一轮考的就在那几张卡里。"""
    text = (question or "").strip()
    if not text:
        return question
    joined = "".join(re.findall(r"[A-Za-z'\-]+", text))
    dictionary = ldict.get_dict()
    for cand in (text, joined):
        if cand and " " not in cand and dictionary.form(cand.lower()):
            return cand
    for cand in (text, joined):
        if not cand or " " in cand:
            continue
        match = difflib.get_close_matches(cand, SEEN_WORDS, n=1, cutoff=0.7)
        if match:
            return match[0]
    return question


def _merge_same_row(lines, gap_ratio=2.5, cy_ratio=0.55):
    """同一行被 OCR 切成两段的拼回去（"D.n." + "世界" -> "D.n.世界"）。"""
    rows = []
    for line in sorted(lines, key=lambda l: (l.cy, l.left)):
        for row in rows:
            ref = row[-1]
            height = max(ref.height, line.height)
            if abs(line.cy - ref.cy) <= height * cy_ratio \
                    and line.left - ref.right <= height * gap_ratio:
                row.append(line)
                break
        else:
            rows.append([line])
    merged = []
    for row in rows:
        row.sort(key=lambda l: l.left)
        text = row[0].text
        for nxt in row[1:]:
            text = _join(text, nxt.text)
        merged.append(TextLine(text,
                               min(l.left for l in row), min(l.top for l in row),
                               max(l.right for l in row) - min(l.left for l in row),
                               max(l.bottom for l in row) - min(l.top for l in row)))
    merged.sort(key=lambda l: (l.top, l.left))
    return merged


_PAGE_KEYWORDS = ("待强化", "再来一遍", "进入强化", "complete", "完成卡包学习",
                  "本次学习", "恭喜", "选择卡包")


def _vision_page_safe(img, vision):
    """读字模型已经把整屏读完，且不像结算/完成/卡包列表页。

    这时 OCR 那次"下半屏补认"可以省掉（约 0.7 秒/张单词卡）；
    只要有一点不像，就照旧补认，别为了省时间漏判结算页。
    """
    if not vision or not vision.get("word") or vision.get("options"):
        return False
    text = " ".join(vision.get("lines") or []).lower()
    if any(k in text for k in _PAGE_KEYWORDS):
        return False
    if any(VISION_OPTION_RE.match(t) for t in vision.get("lines") or []):
        return False      # 只读出 1~3 个选项时 options 为空，但这是题目页，不是单词卡
    return find_teal_button(img) is None


def _vision_card_safe(img, vision):
    """读字模型读完这一屏，且像一张普通单词卡 => 连整屏 OCR 都不用跑。

    进这道门的前提很硬：有单词、不像结算/完成/列表、没有选项痕迹、
    中文行最多一条（卡片上只有标题带中文）、页面没被顶出上沿。
    任何一条不满足就退回整屏 OCR —— 背面就是"多花 1 秒"，点错才是大问题。
    """
    if not _vision_page_safe(img, vision):
        return False
    lines = vision.get("lines") or []
    if sum(1 for t in lines if _cjk_count(t) >= 3) > 1:
        return False      # 选项文字被读掉字母前缀（没有 A./B.）时靠这个兜
    return not _page_scrolled_up(img)


# 释义行的词性标记。故意不含单字母 "a."：选项行 "A. xxx" 也长这样，
# 认错就会把题目当背面，漏答一题。
_BACK_GLOSS_RE = re.compile(
    r"^\s*(n|vt|vi|v|adj|adv|conj|prep|pron|num|art|int|aux)\s*[.．]\s*\S", re.I)


_GLOSS_JUNK = ("例句", "词汇", "强化", "完成", "卡包", "棒棒")


def _pick_gloss_line(vision):
    """从背面整屏读字结果里挑释义行：优先带词性标记的中文行。

    没有词性标记时只收"纯中文"的候选：卡片上的「高频词汇」标签、被读成一行的
    音标 + 「例句」都属于噪声，存进去会拿几个常用字去碰选项，压过词表。
    """
    lines = [t.strip() for t in (vision or {}).get("lines") or []]
    for text in lines:
        if _BACK_GLOSS_RE.match(text) and ldict.CJK.search(text):
            return text
    cands = [t for t in lines
             if ldict.CJK.search(t) and "/" not in t
             and not re.match(r"^[A-Da-d]\s*[.、．:：)）]", t)
             and not re.search(r"[A-Za-z\[\]【】()（）]", t)
             and not any(b in t for b in _GLOSS_JUNK)]
    return max(cands, key=len) if cands else ""


def _vision_back_safe(img, vision):
    """读字模型读到"单词 + 一条带词性的中文释义" => 单词卡背面。"""
    if not vision or not vision.get("word") or vision.get("options"):
        return False
    text = " ".join(vision.get("lines") or []).lower()
    if any(k in text for k in _PAGE_KEYWORDS):
        return False
    if find_teal_button(img) is not None or _page_scrolled_up(img):
        return False
    lines = vision.get("lines") or []
    if sum(1 for t in lines if _cjk_count(t) >= 3) > 3:
        return False      # 4 行中文多半是"选项字母被读掉的题目"，不能当背面
    return bool(_pick_gloss_line(vision))


def _read_card_back(win, front=None):
    """点一下卡片翻到背面，读 app 自己的中文释义。

    翻面没有动画：点完的下一帧就是"单词 + 释义"，不用等画面稳定。
    读完停在背面 —— 背面直接上滑会进下一张（已验证），不用再点回正面。
    读字模型不可用时退回 OCR 读背面；点过去是题目页（这张卡不吃点击）
    就返回空，绝不能把选项行当成释义。
    """
    win.click(CARD_TAP[0], CARD_TAP[1])
    img = _screen(win)
    if img is None:
        return ""
    if front is not None and not _image_changed(front, img):
        img = _screen(win)          # 偶尔第一帧还没切过来，再补拍一张
        if img is None:
            return ""
    vision = vision_screen(img)
    if vision is not None:
        return _pick_gloss_line(vision)
    lines = ocr_lines(img)
    if find_options(lines):
        return ""
    return _pick_gloss_line({"lines": [l.text for l in lines]})


def _chain_tap_safe():
    """串联那一下必须避开题目选项框：万一节奏对不上，点到选项就糟了。"""
    boxes = _OPT_CACHE["boxes"]
    if not boxes:
        return False
    x, y = CHAIN_TAP
    for left, top, bw, bh in boxes:
        if left <= x <= left + bw and top <= y <= top + bh:
            return False
    return True


def _swipe_and_tap(win):
    """上滑进下一张，并在动画中点一下，停在下一张的"单词+释义"帧。

    点一下会把翻页动画停住，所以不用再等画面稳定。
    """
    win.swipe(SWIPE_START, SWIPE_DELTA, duration=CHAIN_SWIPE_DURATION)
    win.click(CHAIN_TAP[0], CHAIN_TAP[1])


def _fast_option_ocr(img):
    """只识别题干+选项区域，返回 (lines, options, question)；不可用返回 None。

    用第一次整屏 OCR 学到的四行选项位置，把顶部进度条和底部空白一起裁掉。
    只在页面没有宽青色按钮（结算/完成/弹窗）时调用；认不出四项、位置对不上、
    或者题干是个不认识的糊词，都退回整屏 OCR —— 宁可慢一点，不能点错。
    """
    if not FAST_OPTION_OCR:
        return None
    if find_teal_button(img) is not None:
        return None                       # 结算/完成/弹窗：交给整屏 OCR 判页型
    boxes = _OPT_CACHE["boxes"]
    if not boxes or _OPT_CACHE["size"] != img.size:
        return None
    w, h = img.size

    # 先用位置缓存做一次很便宜的"这一屏还是不是题目页"检查：
    # 卡片页/被滑走的页面上，这四个位置多半没有字，直接退回整屏 OCR，
    # 避免在单词卡上白跑一遍区域识别。
    probe = [TextLine("", int(l * w), int(t * h), int(bw * w), int(bh * h))
             for l, t, bw, bh in boxes]
    if not _rows_look_right(img, probe):
        return None

    bottom = max((t + bh) for _l, t, _bw, bh in boxes) * h
    # 顶部进度条单独认一小块（很便宜），保证 at_end 和日志里的 "学习x/y" 还在
    progress = _rapid_lines(img, (int(w * 0.30), int(h * 0.06),
                                 int(w * 0.50), int(h * 0.13))) or []
    # 题干 + 选项：从进度条下方开始，到最后一个选项下方一点为止
    rows = _rapid_lines(img, (max(0, int(w * 0.05)), max(0, int(h * 0.14)),
                              min(w, int(w * 0.95)),
                              min(h, int(bottom + h * 0.03))))
    if rows is None:
        return None
    lines = _merge_same_row(list(progress) + list(rows))
    if detect_dialog(img, lines) is not None:
        return None                       # 有"必须要作答"提示：交给整屏 OCR 处理
    options = find_options(lines)
    if len(options) != 4:
        return None
    if not _rows_look_right(img, options):
        return None
    question = pick_question(lines, options)
    if not question:
        return None
    question = snap_to_seen(question)
    # 单个词但词表/已见词都不认识：多半是糊了，交给整屏 OCR 的多遍重认
    word = question.strip().lower().rstrip(".")
    if re.fullmatch(r"[a-z][a-z'\-]{1,40}", word):
        if word not in {x.lower() for x in SEEN_WORDS} \
                and not ldict.get_dict().form(word):
            return None
    return lines, options, question


def _options_from_cached_boxes(img, lines):
    """选项字母没读出来时，用上次学到的四行位置强行裁出 A-D 四行。

    不依赖行首的 "A."：只要四个位置里都有字，就按位置读出中文选项。
    返回 4 行选项；位置对不上/读不全就返回空。
    """
    boxes = _OPT_CACHE["boxes"]
    if not boxes or _OPT_CACHE["size"] != img.size:
        return []
    w, h = img.size
    probe = [TextLine("", int(l * w), int(t * h), int(bw * w), int(bh * h),
                      letter="ABCD"[i])
             for i, (l, t, bw, bh) in enumerate(boxes)]
    if len(probe) != 4 or not _rows_look_right(img, probe):
        return []
    opts = refine_options(img, probe)
    if len(opts) == 4 and all(o.text.strip() for o in opts):
        return opts
    return []


def _likely_question(img, state):
    """这一屏大概是不是题目页（即使选项字母没读出来）。

    两个依据：四个选项位置里有字；或者按 5 卡 + 5 题的节奏，这一步应该出题了。
    """
    boxes = _OPT_CACHE["boxes"]
    if boxes and _OPT_CACHE["size"] == img.size:
        w, h = img.size
        probe = [TextLine("", int(l * w), int(t * h), int(bw * w), int(bh * h))
                 for l, t, bw, bh in boxes]
        if _rows_look_right(img, probe):
            return True
    if state and not state.get("reinforce"):
        qrun = state.get("qrun", 0)
        crun = state.get("crun", 0)
        if 1 <= qrun <= 4 or crun >= 5:
            return True
    return False


def inspect(win, img=None):

    """截图 -> 读字模型 -> （必要时）整屏 OCR。

    选项四行的位置是固定的：第一次用 OCR 学到后，后面每屏只让读字模型读文字、
    位置直接复用，不再整屏识别。单词卡只要读字模型读得全，也完全不跑 OCR。
    读不出四项 / 缓存位置对不上 / 结算页 / 页面被顶出上沿等，都自动退回原来的
    整屏 OCR，只会慢一点，不会点错。
    """
    if img is None:
        img = win.grab_content()
    else:
        # 传进来的帧可能已经过期（app 在这期间跳到下一页了）。
        # 先拍一张新的比一下：不一样就用新的，避免"拿旧画面做判断"。
        fresh = win.grab_content()
        if _image_changed(img, fresh):
            img = fresh
    vision = vision_screen(img) if USE_VISION else None
    rows = _cached_option_rows(img, vision)
    if rows is not None:
        question = (vision or {}).get("word") or ""
        return img, _vision_text_lines(vision), rows, question, vision
    if _vision_card_safe(img, vision):
        # 单词卡：读字模型已经把整屏文字读完，位置也用不上 —— 整屏 OCR 直接跳过
        question = vision["word"]
        return img, _vision_text_lines(vision), [], question, vision
    if _vision_back_safe(img, vision):
        # 卡片背面（串联上滑后直接落到这里）：同样不用整屏 OCR
        question = vision["word"]
        return img, _vision_text_lines(vision), [], question, vision
    # 纯 OCR 模式：位置学过且这屏没有结算/完成/弹窗按钮时，只认题干+选项区域
    if not vision:
        fast = _fast_option_ocr(img)
        if fast is not None:
            lines, options, question = fast
            return img, lines, options, question, vision
    lines = _merge_same_row(
        ocr_lines(img))
    options = find_options(lines)
    if options:
        if not (vision and len(vision.get("options") or []) == len(options) == 4):
            options = refine_options(img, options)
        if len(options) == 4:
            _remember_option_boxes(img, options)
        if vision and vision.get("word"):
            question = vision["word"]
        else:
            question = pick_best_question(question_candidates(img, options, lines))
            question = snap_to_seen(question)
    else:
        question = ((vision or {}).get("word") or pick_question(lines, [])
                    or refine_word(img))
    return img, lines, options, question, vision


def handle_screen(win, dry=False, img=None, recovered=False, state=None,
                  settle_retry=0):

    """看一眼屏幕并处理。返回 (状态, 画面指纹)。

    状态：题目-已答 / 题目-没把握 / 单词卡 / 强化单词卡 / 弹窗 / 出错
    弹窗会先点掉再看一眼，所以同一屏可能处理两步。
    img 传上一步留下的稳定帧可以省一次截图（比如刚滑完页那帧）；
    只用于第一遍识别，点掉弹窗后的第二遍必须重新截屏。
    recovered=True 表示已经做过"往回滑恢复页面"，不再重复。
    state 是 walk 持有的运行状态：点过结算页「继续」后标记强化轮，
    强化轮的页面被顶出上沿时用它配合滚轮恢复。
    """
    dialog_seen = False         # 这一屏点掉过"必须要作答"弹窗 => 肯定是题目页
    for _attempt in range(5):
        img, lines, options, question, vision = inspect(
            win, img if _attempt == 0 else None)
        w, h = img.size
        prog = screen_progress(lines)
        prefix = ("[%d/%d] " % prog) if prog else ""

        dialog = detect_dialog(img, lines)
        if dialog is not None:
            hint, button = dialog
            print("%s弹窗「%s」" % (prefix, hint.text.strip()))
            if dry:
                print("    （走查模式，不点）确认按钮: %s"
                      % (str(button) if button else "没找到"))
                return "弹窗", (hint.text,)
            if button is None:
                print("    看到提示但没找到「确认」按钮，按普通界面继续处理")
            else:
                print("    -> 点确认 (%.3f, %.3f)" % (button.cx / w, button.cy / h))
                win.click(button.cx / w, button.cy / h)
                time.sleep(AFTER_CONFIRM_WAIT)
                dialog_seen = True
                continue                  # 点掉之后再认一次屏

        tut = _tutorial_hint(lines)
        if not options and (tut or _tutorial_dark(img)):
            # 新开一包/重开时会出现新手引导（深色遮罩 + 手势提示），
            # 不处理的话会在 0/25 第一张卡上反复滑、看起来像卡住。
            if dry:
                print("%s新手引导（走查不操作）：%s" % (prefix, tut or "深色遮罩"))
                return "教程", (tut,)
            # 点提示框即可推进：第一步“点击页面翻转”，第二步“上滑下一个单词”，
            # 实测连点两次提示框都能推进/关闭，而且不会把页面滚出上沿。
            print("%s新手引导：点提示框继续（%s）" % (prefix, tut or "深色遮罩"))
            win.click(0.5, 0.73)
            time.sleep(1.1)
            continue

        if not options and dialog_seen and not dry and not recovered:
            # "必须要作答"弹窗只会在没答的题目页上出现 —— 这屏肯定是题目。
            # 点掉弹窗却还看不到选项，多半页面被滑得不正常。往回滑恢复，
            # 千万别当单词卡再滑（一滑弹窗又出来，死循环）。
            print("%s点掉弹窗还是看不到选项，先往回滑恢复页面" % prefix)
            win.swipe((0.5, 0.35), (0.0, 0.22))
            _wait_stable(win)
            return handle_screen(win, dry=dry, recovered=True, state=state)

        signature = (tuple(o.text for o in options), question)
        if is_pack_list(lines):
            print("%s已经回到卡包列表，这一包跑完了" % prefix)
            return "结束", signature

        if is_final_page(lines):
            # 整个包跑完了（大字 COMPLETE）：点「完成」后直接收工，跳到哪不用管。
            # 注意：只要能看见「完成」按钮就直接点 —— 识别和点击之间不做任何
            # 滑动/滚动，否则这一下会把页面带走，按钮位置也跟着变。
            button = find_teal_button(img)
            if button is not None:
                print("%s完成页（COMPLETE），点「完成」" % prefix)
                if dry:
                    print("    （走查模式，不点）按钮: %s" % str(button))
                    return "结算页", signature
                win.click(button.cx / w, button.cy / h)
                print("    -> 已点「完成」")
                return "结束", signature
            # 只有按钮压根看不见（页面被顶出上沿）才滚轮恢复一次，恢复完重新识别
            if not dry and not recovered and _page_scrolled_up(img):
                print("%s完成页被顶出上沿（按钮不在视野），滚轮恢复" % prefix)
                _restore_reinforce_page(win)
                return handle_screen(win, dry=dry, recovered=True, state=state)
            print("%s完成页（COMPLETE），但没找到「完成」按钮，停下" % prefix)
            return "出错", signature

        if is_summary(lines):
            button = find_teal_button(img)
            print("%s结算页（待强化单词），点「继续」" % prefix)
            if dry or button is None:
                if button is None:
                    print("    没找到「继续」按钮，停下")
                    return "出错", signature
                return "结算页", signature
            win.click(button.cx / w, button.cy / h)
            time.sleep(2.0)
            if state is not None:
                # 进入强化轮：学习轮的 5 题节奏计数必须清零，
                # 否则 _likely_question 会把强化轮第一张卡当成“题目”。
                state["reinforce"] = True
                state["qrun"] = 0
                state["crun"] = 0
                state["aligned"] = False
            forget_option_boxes()          # 强化轮版面和答题轮不一样，重新学位置
            continue                      # 点完再看一眼（可能进入强化轮）


        if not options and _page_scrolled_up(img) and not dry and not recovered:
            # 先确认真的滑出上沿了：上滑动画中间那一帧也会让内容贴顶，
            # 不能一看到就往下滑，否则会和正在进行的滑动打架。
            settled = _wait_stable(win, timeout=1.2)
            if settled is not None and _page_scrolled_up(settled):
                # 页面被滑出上沿（卡片顶到屏幕上边、下面一大片空白）：先往下滑回来，
                # 别当单词卡再上滑，否则只会越滑越远，看起来就像卡住。
                print("%s页面被滑出上沿，往回滑恢复" % prefix)
                win.swipe((0.5, 0.30), (0.0, 0.22))
                _wait_stable(win)
                return handle_screen(win, dry=dry, recovered=True, state=state,
                                     settle_retry=settle_retry)
            if settled is not None and settle_retry < 1:
                # 动画已经结束在正常页面：用新帧重认一次
                return handle_screen(win, dry=dry, recovered=recovered,
                                     state=state, settle_retry=settle_retry + 1)

        if not options and not dry:
            likely = _likely_question(img, state)
            if likely:
                settled = _wait_stable(win, timeout=1.2)
                if (settled is not None and _image_changed(img, settled)
                        and settle_retry < 1):
                    return handle_screen(win, dry=dry, recovered=recovered,
                                         state=state,
                                         settle_retry=settle_retry + 1)
                recovered_opts = _options_from_cached_boxes(img, lines)
                if recovered_opts:
                    options = recovered_opts
                    question = pick_question(lines, options) or question
                    question = snap_to_seen(question)
                    signature = (tuple(o.text for o in options), question)
                    print("%s选项字母没读出来，按位置补认" % prefix)
                else:
                    print("%s像是题目，但选项没认出来，先停下" % prefix)
                    return "题目-没把握", signature

        if options:

            if looks_answered(img):
                print("%s这一屏已经答过了（有对错高亮），滑下一题" % prefix)
                return "题目-已答", signature

            if not dry and not recovered:
                # 强化轮的题目是从下方滑入的：刚识别完页面可能还在动，
                # 这时算出来的行坐标是"飞行中"的，点下去就会落到隔壁一行。
                # inspect 里 OCR + 视觉已经花掉约 1.5 秒，动画基本放完；
                # 这里只补拍一张比对，变了就按新画面重认（比原来少截一张图）。
                fresh = _screen(win)
                if fresh is not None and _image_changed(img, fresh):
                    img, lines, options, question, vision = inspect(win)
                    w, h = img.size
                    if not options:
                        print("%s页面刚动过，重新识别后已经没有选项了" % prefix)
                        return "未知", signature
                    if looks_answered(img):
                        print("    这一屏已经答过了，直接滑")
                        return "题目-已答", signature

            # 视觉结果已经由 inspect 在 OCR 期间并行取回来了（题干/单词比 OCR 准）
            if vision and vision.get("word"):
                question = vision["word"]
            if vision and VISION_ROLE == "read":
                # 读字模型的中文选项比 Tesseract 准，换掉选项文字再判题
                _apply_vision_options(options, vision)

            if not question and not dry and not recovered:
                # 有选项却没题干：多半是页面被滑得滚出了屏幕（题干看不见了）。
                # 往回滑把页面恢复、重新走一遍识别，别急着蒙——
                # 页面不正常时点选项没有反应，还会把页面越滑越乱。
                print("%s有选项但没认出题干，先往回滑恢复页面" % prefix)
                win.swipe((0.5, 0.35), (0.0, 0.22))
                _wait_stable(win)
                return handle_screen(win, dry=dry, recovered=True, state=state)

            if not question and not (vision and vision.get("answer")):
                index = _unsure_index(options)
                if index is None:
                    print("%s题目（没认出题干），不点了" % prefix)
                    return "题目-没把握", signature
                line = options[index]
                print("%s没认出题干，蒙一个：选项%s「%s」"
                      % (prefix, line.letter or "ABCD"[index], line.text[:18]))
                score, source = 0.0, "蒙的"
            else:
                index, score, source = choose(question, options, vision)
                if score <= 0 and not dry:
                    # 刚滑过来的那一屏可能还没渲染完，等一下再看一遍
                    time.sleep(0.8)
                    img, lines, options, question, vision = inspect(win)
                    w, h = img.size
                    if looks_answered(img):
                        print("    这一屏已经答过了，直接滑")
                        return "题目-已答", signature
                    if options and question:
                        # 新帧的视觉结果也一起换了：上一帧可能停在翻页动画中间
                        # （题干还是上一张卡、选项已是新题），别拿旧帧的结论硬选
                        if vision and vision.get("word"):
                            question = vision["word"]
                        if vision and VISION_ROLE == "read":
                            _apply_vision_options(options, vision)
                        index, score, source = choose(question, options, vision)
                line = options[index]
                print("%s题目: %-22s -> 选项%s「%s」 %.2f（%s）"
                      % (prefix, question[:22], line.letter or "ABCD"[index],
                         line.text[:18], score, source))
                if dry:
                    return "题目-已答", signature
                if score <= 0:
                    by_pos = choose_by_pos(question, options)
                    if by_pos is not None:
                        index, source = by_pos, "按词性推断"
                        line = options[index]
                        print("    字面对不上，按词性定为选项%s「%s」"
                              % (line.letter or "ABCD"[index], line.text[:18]))
                    else:
                        guess = _unsure_index(options)
                        if guess is None:
                            return "题目-没把握", signature
                        index, source = guess, "蒙的"
                        line = options[index]
                        print("    没查到词、词性也不唯一，蒙一个：选项%s「%s」"
                              % (line.letter or "ABCD"[index], line.text[:18]))

            if dry:                        # 走查模式：只报告，不点
                return "题目-已答", signature
            win.click(line.cx / w, line.cy / h)
            after = _wait_change(win, img)
            settled = _wait_stable(win, timeout=0.9)
            if settled is not None:
                after = settled
            if not _image_changed(img, after):
                print("    画面没变，再点一次")
                win.click(line.cx / w, line.cy / h)
                after = _wait_change(win, img)
                settled = _wait_stable(win, timeout=0.9)
                if settled is not None:
                    after = settled
                if not _image_changed(img, after):
                    print("    点了两次都没反应（可能这题早就答过了）")
                    forget_option_boxes()
                    return "题目-已答", signature


            # 点完题那一帧里已经带着对错高亮：顺手学一下正确答案（答错才记）。
            learned = learn_from_answer(after, options, question, index)
            if learned is not None:
                print("    答错了，记下正确项%s「%s」到错题本"
                      % (options[learned].letter or "ABCD"[learned],
                         options[learned].text[:18]))

            # 最后一题（包括强化轮最后一题）不在这里等：
            # 答完直接返回，walk 会按正常节奏上滑；下一屏由 handle_screen
            # 识别成结算页/完成页后再点「继续」/「完成」。
            return "题目-已答", signature

        # 卡片上的单词用 inspect 并行取回的视觉结果（比 OCR 裁切放大更稳）
        vword = (vision or {}).get("word") or ""
        shown = vword or question
        if shown and " " not in shown.strip():
            remember_word(shown)                    # 记下见过的词，题干认糊时用来对
        if shown:
            if (state is not None and state.get("reinforce")) \
                    or _reinforce_tab_active(img):
                # 强化轮（点过结算页「继续」，或标签栏"强化"亮着）。它的页面有
                # 两种形态：正常居中（和学习轮一样，标准上滑即可）；被顶出上沿
                # （词贴着 y=0 被切一半，OCR 只剩乱码）—— 这时先用滚轮把内容
                # 带回中央再重新识别。上滑起点若落在卡片外的空白处，永远滑不动。
                #
                # 只有"词根本认不出来"时才滚轮恢复：位置偏上不代表页面坏了，
                # 刚点完「继续」进强化轮时页面还在过渡，光看位置会误判，
                # 白滚一下反而把正常页面顶上去。
                # 这里仍用 OCR 的 question 判断，避免视觉模型在坏页面上"猜"出词。
                if not recovered and not _word_readable(question) \
                        and _page_scrolled_up(img):
                    # 上滑动画中间也会"像"被顶出上沿；先等画面稳定再决定要不要滚轮
                    settled = _wait_stable(win, timeout=1.2)
                    if settled is not None and not _page_scrolled_up(settled):
                        if _image_changed(img, settled) and settle_retry < 1:
                            return handle_screen(win, dry=dry,
                                                 recovered=recovered, state=state,
                                                 settle_retry=settle_retry + 1)
                    else:
                        print("%s强化轮页面认不出词，滚轮恢复" % prefix)
                        _restore_reinforce_page(win)
                        return handle_screen(win, dry=dry, recovered=True,
                                             state=state)

                print("%s强化轮单词卡: %s" % (prefix, shown))
                if not dry:
                    # 强化轮的题按"app 自己的释义"判：屏上已是背面就直接读，
                    # 还在正面就点一下翻过去读（和学习轮同一套翻面逻辑）。
                    # 只进本次判题缓存，不写错题本
                    gloss = _pick_gloss_line({"lines": [l.text for l in lines]})
                    if not gloss:
                        gloss = _read_card_back(win, img)
                    key = shown.strip().lower()
                    if gloss and _CARD_GLOSS.get(key) != gloss:
                        _CARD_GLOSS[key] = gloss
                        print("    背面释义: %s" % gloss)
                return "强化单词卡", signature
            print("%s单词卡: %s" % (prefix, shown))
            if _vision_back_safe(img, vision):
                # 串联上滑后直接落在背面：释义已经在手，不要再点一下翻回正面
                gloss = _pick_gloss_line(vision)
                if gloss:
                    _CARD_GLOSS[shown.strip().lower()] = gloss
                    print("    背面释义: %s" % gloss)
            elif not dry and USE_VISION and VISION_ROLE == "read":
                # 背面就是 app 自己的释义，读下来给这一轮的题目当标准答案。
                # 读完停在背面，后面照常上滑进下一张。
                gloss = _read_card_back(win, img)
                if gloss:
                    _CARD_GLOSS[shown.strip().lower()] = gloss
                    print("    背面释义: %s" % gloss)
            return "单词卡", signature
        print("%s没认出这是什么界面" % prefix)
        return "未知", signature

    return "出错", None


def _screen(win):
    try:
        return win.grab_content()
    except Exception:
        return None


def _wait_stable(win, timeout=3.5, interval=0.05, threshold=0.2):
    """等画面停下来再截图 —— 滑动/跳转有动画，动画中截的图坐标是"飞行中"的，
    按那个坐标去点就会点到隔壁选项。

    先截图再歇：截图本身要 0.2 秒出头，轮询节奏已经够慢，固定 sleep 只会
    把每一步的等待时间再抻长。
    """
    prev = _screen(win)
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = _screen(win)
        if prev is not None and not _image_changed(prev, cur, threshold):
            return cur
        prev = cur
        time.sleep(interval)
    return prev


def _wait_change(win, before, timeout=4.0, interval=0.05, threshold=0.2):
    """等画面真的变掉（滑动的效果常常延迟 1~2 秒才画出来）。

    不能用 _wait_stable：它看到"两帧一样"就返回，而滑动还没生效时画面恰好在
    静止的旧状态，于是把上一屏当成当前屏 —— 下一步又判成"已答过"，重复滑一次。
    这里改成"等它和滑动前不一样"，等不到才返回当前帧（由调用方按失败处理）。
    """
    deadline = time.time() + timeout
    cur = before
    while time.time() < deadline:
        nxt = _screen(win)
        if nxt is None:
            time.sleep(interval)
            continue
        cur = nxt
        if before is None or _image_changed(before, nxt, threshold):
            return nxt
        time.sleep(interval)
    return cur


def _wait_settle(win, before, change_timeout=4.0, stable_timeout=1.5):
    """先等画面变化（滑动生效），再等它停稳。

    只在"画面变了"就识别，会把滑动动画中间的那一帧当成新页面：
    题目还在上滑、选项还没进入视口时，很容易被认成单词卡；
    卡片被上滑到一半时，又容易误判成"页面被滑出上沿"。
    """
    changed = _wait_change(win, before, timeout=change_timeout)
    if changed is None:
        return None
    settled = _wait_stable(win, timeout=stable_timeout)
    return settled if settled is not None else changed



def _page_scrolled_up(img):
    """强化轮的页面被顶出上沿时，内容只剩上半截：卡片底缘约在半屏处，下面全空白。

    从下往上扫，找到最后一行有内容的像素位置；内容底在半屏以上 = 被顶出了。
    （触摸手势滚不动这种页面，只有滚轮有效，所以要先识别出来再对症恢复。）
    """
    w, h = img.size
    x0, x1 = int(w * 0.35), int(w * 0.65)
    gray = img.convert("L")
    for y in range(int(h * 0.95), int(h * 0.30), -4):
        row = gray.crop((x0, y, x1, y + 1))
        hist = row.histogram()
        if sum(hist[:200]) > (x1 - x0) * 0.05:      # 这一行有内容（非近白像素）
            return y < h * 0.6
    return False


def _word_readable(word):
    """词到底认出来没有：至少有一个 3 字母以上、含元音的字母串。

    页面被顶出上沿时 OCR 通常只剩一两个字符的碎片（"ru"、"l"），
    用这个判断比"位置高不高"可靠得多。
    """
    if not word:
        return False
    tokens = re.findall(r"[A-Za-z]{3,}", word)
    if not tokens:
        return False
    text = "".join(tokens).lower()
    return sum(1 for ch in text if ch in "aeiou") >= 1


def _restore_reinforce_page(win):
    """强化轮的页面常被顶出上沿（词贴着屏幕顶被切一半，OCR 全废）。

    触摸手势滚不动它，只有滚轮有效，且滚轮必须落在卡片上：
    先 tap 定位到卡片（会翻面，无副作用），再连续向"上"滚（负 dy）把内容带下来。
    """
    if win.resolve_mode() != "adb" or win.channel is None:
        return                       # 只有 ADB 通道能发滚轮事件
    win.click(0.5, 0.23)
    time.sleep(0.25)
    for _ in range(10):
        win.channel.run(["shell", "input", "roll", "0", "-2000"], timeout=10)
        time.sleep(0.12)
    time.sleep(0.8)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _next_chain_ok(state, status, ready):
    """按"5 张词卡 + 5 道题"的节奏判断：这一滑之后是不是还是一张词卡。

    只有确定下一屏是词卡，才敢在滑动动画中补点一下（直接落到下一张的背面）。
    节奏没对齐时（刚启动、刚进强化轮、漏看/多看了一张卡）一律不串联 ——
    最坏的结果只是多滑一次，不会点到题目选项。

    qrun 数的是上一个题块里连着答过几题；连着 5 题之后的第一张卡就是
    新词块的第 1 张，从这时开始计数才有意义。
    """
    if state.get("reinforce"):
        state["aligned"] = False
        return False
    if status == "单词卡":
        if state.get("qrun", 0) >= 5:
            state["aligned"] = True
            state["crun"] = 1
            state["qrun"] = 0
        elif state.get("aligned"):
            state["crun"] = state.get("crun", 0) + 1
        else:
            state["crun"] = 0
        crun = state.get("crun", 0)
        if crun >= 5:
            state["aligned"] = False
        return bool(ready and 1 <= crun <= CHAIN_LAST_CARD)
    if status.startswith("题目"):
        state["qrun"] = state.get("qrun", 0) + 1
        state["crun"] = 0
        state["aligned"] = state.get("qrun", 0) >= 5
        return False
    # 结算页/弹窗/认不出：节奏已经断了，计数清零重新对齐
    state["aligned"] = False
    state["qrun"] = 0
    state["crun"] = 0
    return False


def walk(win, steps, dry=False):
    """每一步：看一眼 -> 该点就点 -> 滑一下。

    这个应用答完不会自动跳，答完/看完都要上滑才走 —— 所以每步末尾统一滑，
    而不是"答完就不滑"（那样下一屏还会是同一题，再点就毫无反应）。

    主步数用完时如果还没回到卡包列表（通常正停在"强化"结算页，或者强化轮
    还没走完），会自动收尾：点「继续」把之前答错的词重新过一遍，直到真正
    一包结束。收尾步数有上限（TAIL_STEPS），防意外死循环。
    """
    answered = 0
    unknown_run = 0
    stuck, last = 0, None
    stable = None             # 上一步滑动后等到的稳定帧，下一步直接拿去识别，省一次截屏
    reason = "预算用完"        # 主循环怎么退出的：决定要不要收尾
    state = {"reinforce": False, "qrun": 0, "crun": 0, "aligned": False}
    #         点过结算页「继续」=> 强化轮（里面的单词卡不上滑）

    def run_phase(phase, count):
        nonlocal answered, unknown_run, stuck, last, stable, reason
        for i in range(1, count + 1):
            print("-- %s 第 %d/%d 步 --" % (phase, i, count))
            try:
                # 不再复用上一步的旧帧：app 切页有时比"等稳定"还慢，
                # 拿旧帧判断会误判成"这一屏已经答过了"，白滑一下。
                status, signature = handle_screen(win, dry=dry, state=state)
            except Exception as exc:
                print("    出错: %s" % exc)
                status, signature = "出错", None

            if status == "题目-已答" and not dry:
                answered += 1
            if status == "结束":
                print("\n一包结束，干净退出。")
                reason = "结束"
                return
            if status == "未知":
                unknown_run += 1
                if unknown_run >= UNKNOWN_LIMIT:
                    print("\n连续 %d 次认不出界面，先停下（不乱滑）。" % UNKNOWN_LIMIT)
                    print("  可以：手动切到卡包/题目页，或跑 --走查 看看它看到了什么")
                    reason = "认不出界面"
                    return
            else:
                unknown_run = 0

            if status == "题目-没把握" and not dry:
                print("\n这题没把握，为了不乱选先停下。可以：")
                print("  · 用 --走查 看看它认成了什么")
                print("  · 或者手动答掉这一题再继续")
                reason = "没把握"
                return
            if status == "出错" and not dry:
                print("\n出错了，先停下")
                reason = "出错"
                return

            if status == "结算页" and not dry:
                # 刚点完「继续」，app 正在切进强化轮：不要再滑，等它切完
                # 强化轮版面/节奏都不一样，串联计数清零
                state.update(aligned=False, crun=0, qrun=0)
                stable = _wait_settle(win, _screen(win), change_timeout=3.0)
                last = signature
                continue

            if status == "强化单词卡" and not dry:
                # 强化轮：app 自己会往下走，别急着上滑（上滑会把词滚出识别范围，
                # 之后还得靠滚轮恢复）。先等一会儿，画面真没动才补一滑。
                before = _screen(win)          # 现拍一张，别用可能过期的旧帧
                time.sleep(0.8)
                settled = _wait_stable(win, timeout=1.5)
                moved = (before is not None and settled is not None
                         and _image_changed(before, settled))
                if moved:
                    stable = settled            # app 自己走了一步：这一步不滑
                    stuck = 0
                    last = signature
                    continue
                win.swipe(SWIPE_START, SWIPE_DELTA)
                stable = _wait_settle(win, before)
                if signature is not None and signature == last:
                    stuck += 1
                    if stuck >= STUCK_LIMIT:
                        print("\n强化轮的卡片反复滑不动，先停下")
                        reason = "画面没变化"
                        return
                else:
                    stuck = 0
                last = signature
                continue

            ready = (not dry and CHAIN_CARD_TAP and win.mode == "adb"
                     and USE_VISION and VISION_ROLE == "read"
                     and _chain_tap_safe())
            chain = _next_chain_ok(state, status, ready)
            if chain:
                # 下一屏确定还是词卡：滑动动画中补点一下，直接停在它的背面。
                # 点不到/点歪了也只是多一次普通滑动，不会选错答案。
                _swipe_and_tap(win)
                print("    （串联：直接落到下一张背面）")
                stable = None
            else:
                before = _screen(win)          # 滑动前的画面，用来判断"滑到底生效没有"
                win.swipe(SWIPE_START, SWIPE_DELTA)
                stable = _wait_settle(win, before)

            if signature is not None and signature == last:
                stuck += 1
                time.sleep(1.5)      # 连续没变化：多半是 App 在过渡期吞输入，歇一下再滑
                if stuck >= STUCK_LIMIT:
                    print("\n画面连续 %d 次没变化，滑动好像没生效（或者已经到底了），先停下。"
                          % STUCK_LIMIT)
                    reason = "画面没变化"
                    return
            else:
                stuck = 0
            last = signature

    run_phase("主", steps)
    if reason == "预算用完" and not dry:
        run_phase("收尾", TAIL_STEPS)
    return answered


def main(argv):
    debug = "--调试" in argv or "--debug" in argv
    dry = "--走查" in argv or "--dry" in argv

    global VISION_MODEL, VISION_ROLE, USE_VISION
    for i, arg in enumerate(argv):
        if arg in ("--模型", "--model") and i + 1 < len(argv):
            VISION_MODEL = argv[i + 1]
            # OCR 模型只会认字，判题交词表/文本模型；其余模型按视觉判题用
            VISION_ROLE = "read" if "ocr" in VISION_MODEL.lower() else "judge"
            USE_VISION = True   # 标准版默认关，指定了模型才开
            break
    if "--无视觉" in argv:
        USE_VISION = False
    if USE_VISION and not _model_present(VISION_MODEL):
        print("视觉模型 %s 不可用：先启动 Ollama 并确认拉过这个模型。" % VISION_MODEL)
        print("标准版默认离线判题，不需要它：去掉 --模型 参数即可。")
        return 1

    if OCR_ENGINE == "rapid" and not rapid_ocr.available():
        print("RapidOCR 不可用：deps\\site-packages_rapid 或 deps\\rapidocr_models 缺文件。")
        return 1
    try:
        win = ldwin.get_window()
        win.resolve_mode()
        print("窗口: %r  %s" % (win.title, win.describe()))
        if win.mode_reason:
            print("说明: %s" % win.mode_reason)
        box = win.content_box(refresh=True)
    except Exception as exc:
        print(exc)
        return 1
    if USE_VISION:
        print("画面: %dx%d  判题: %s  看图: %s（%s）  文本判题: %s"
              % (box.w, box.h, MATCH_MODE, VISION_MODEL,
                 "只读字" if VISION_ROLE == "read" else "直接判题", LLM_MODEL))
    else:
        print("画面: %dx%d  判题: 离线词表"
              % (box.w, box.h))
    if MATCH_MODE != "api":
        dic = ldict.get_dict()
        print("词表: %s  收录 %d 条"
              % (dic.path or "（没找到 词典.tsv.gz，判题会跳过）", len(dic)))
    win.activate()

    if debug:
        img, lines, options, question, vision = inspect(win)
        if vision:
            print("视觉模型: 题干/单词=%r  答案=%r  选项=%r"
                  % (vision.get("word"), vision.get("answer"), vision.get("options")))
            if vision.get("word"):
                question = vision["word"]
        print("识别到 %d 行文字：" % len(lines))
        for line in lines:
            tag = ""
            for i, opt in enumerate(options):
                if opt.text == line.text and abs(opt.top - line.top) < 3:
                    tag = "  <== 选项%s" % "ABCD"[i]
            print("   " + str(line) + tag)
        print("界面类型: %s   题干: %r"
              % ("题目" if options else "单词卡", question))
        if vision and VISION_ROLE == "read" and options:
            _apply_vision_options(options, vision)
        if options and question:
            print(ldict.explain([o.text for o in options], question))
            index, score, source = choose(question, options, vision)
            print("判定: 选项%s（%s，%.2f）"
                  % (options[index].letter or "ABCD"[index], source, score))
        _save_debug(img, lines, options, question)
        return 0

    if dry:
        count = next((int(a) for a in argv if a.isdigit()), STEPS_PER_ROUND)
        print("走查 %d 步（只识别、不点击）" % count)
        walk(win, count, dry=True)
        print("\n走查结束")
        return 0

    raw = next((a for a in argv if a.isdigit()), None)
    if raw:
        steps, rounds = int(raw), 1
    else:
        rounds = ROUNDS
        steps = ROUNDS * STEPS_PER_ROUND

    print("共 %d 步（%d 轮 × %d 步）" % (steps, rounds, steps // max(1, rounds)))
    try:
        answered = walk(win, steps)
    except KeyboardInterrupt:
        print("\n已手动中断")
        return 0
    print("\n程序运行结束：共作答 %d 题" % answered)
    return 0


def _save_debug(img, lines, options, question):
    from PIL import ImageDraw
    canvas = img.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    for line in lines:
        draw.rectangle([line.left, line.top, line.right, line.bottom],
                       outline=(150, 150, 150))
    for i, line in enumerate(options):
        draw.rectangle([line.left, line.top, line.right, line.bottom],
                       outline=(255, 0, 0), width=3)
        draw.text((line.left + 4, max(0, line.top - 14)), "选项%s" % "ABCD"[i],
                  fill=(255, 0, 0))
    path = "调试_答题.png"
    canvas.save(path)
    print("已保存 %s（红框=识别到的选项）  题干=%r" % (path, question))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


