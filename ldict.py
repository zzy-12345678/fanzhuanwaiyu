# -*- coding: utf-8 -*-
"""ldict.py —— 离线英汉词表 + 选项匹配（不联网、不调翻译 API）

思路：题目是英文，选项是中文，所以只要能把英文词换成中文含义，就能和选项比对。
这里用本地词表（ECDICT 精简版，词典.tsv.gz）做"逐词释义"，再把 4 个选项的
中文重合度打分，挑最像的那个 —— 效果上等价于原来的"翻译后比对"，
但完全离线、不用 key、没有配额、也不怕断网。

对单词题（一个词 + 4 个中文释义）命中率最高；
整句题也能用（按实词释义的覆盖度算），只是比整句翻译略粗。
"""

from __future__ import annotations

import gzip
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DICT_CANDIDATES = ("词典.tsv.gz", "词典.tsv")

# 英文虚词：没有信息量，查出来只会稀释匹配度
STOPWORDS = set("""
a an the and or but if then than that this these those there here
is am are was were be been being do does did done have has had having
i me my we us our you your he him his she her it its they them their
to of in on at by for with from into onto about as so not no nor
will would shall should can could may might must ought
which who whom whose what when where why how all any both each few
more most other some such only own same very just also too
s t d ll re ve m
""".split())

POS_RE = re.compile(r"^(art|n|v|vt|vi|adj|adv|prep|pron|conj|num|int|aux|abbr|a)\s*[.．]\s*")
CJK = re.compile(r"[\u4e00-\u9fff]")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

# 单字兜底时忽略这些虚字，它们几乎不能区分选项
CHAR_STOP = set("的了着地得使把被将在于与其之而且或所也都还很更最一二三四五六七八九十")


# --------------------------------------------------------------------------
# 词表
# --------------------------------------------------------------------------
class Dictionary:
    def __init__(self, path=None):
        self.path = path or self._locate()
        self.words = {}
        self._load()

    @staticmethod
    def _locate():
        for name in DICT_CANDIDATES:
            path = os.path.join(HERE, name)
            if os.path.isfile(path):
                return path
        return None

    def _load(self):
        if not self.path:
            return
        opener = gzip.open if self.path.endswith(".gz") else open
        with opener(self.path, "rt", encoding="utf-8") as f:
            for line in f:
                word, _, gloss = line.partition("\t")
                if gloss:
                    self.words[word] = gloss.strip()

    def __len__(self):
        return len(self.words)

    def form(self, word):
        """这个词的中文释义；带简单的词形还原（复数/过去式/ing…）。"""
        word = word.lower().strip("'")
        for variant in _variants(word):
            gloss = self.words.get(variant)
            if gloss:
                return gloss
        return ""


_dict = None


def get_dict():
    global _dict
    if _dict is None:
        _dict = Dictionary()
    return _dict


def _variants(word):
    yield word
    rules = (("ies", "y"), ("ied", "y"), ("ves", "f"), ("es", ""), ("s", ""),
             ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"), ("ly", ""),
             ("er", ""), ("er", "e"), ("est", ""), ("est", "e"))
    for suffix, repl in rules:
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            yield word[:-len(suffix)] + repl
    # stopped -> stop / running -> run
    stem = word
    for suffix in ("ed", "ing", "er", "est"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    if len(stem) > 3 and stem[-1] == stem[-2]:
        yield stem[:-1]
        yield stem


# --------------------------------------------------------------------------
# 中文释义 -> 词条
# --------------------------------------------------------------------------
def gloss_terms(gloss):
    """把 "v. 放弃；抛弃，遗弃" 拆成 ["放弃", "抛弃", "遗弃"]。

    单字释义（"吨"、"霜"）也要留：ton / frost 这类词整条释义就一个字，
    全丢掉就等于"查不到词"。它们权重低，而且被多个选项共享时会被降权。
    """
    terms = []
    for part in re.split(r"[；;，,、/（）()\[\]【】\s]+", gloss):
        part = POS_RE.sub("", part).strip()
        part = part.strip("了个.")
        # 只把长释义末尾的"的"当语气词去掉；"我的/你的/他的"这种两字词
        # 去掉"的"就只剩"我/你/他"，四个选项会全部撞成同分。
        if len(part) > 2:
            part = part.strip("的")
        if not part or not CJK.search(part):
            continue
        if part in ("等等", "某人", "某物", "通常", "常与", "表示", "用于"):
            continue
        terms.append(part)
    return terms


POS_SHORT = {"n": "n", "vt": "v", "vi": "v", "v": "v", "adj": "a", "a": "a",
             "adv": "adv", "conj": "conj", "prep": "prep", "pron": "pron",
             "num": "num", "art": "art", "int": "int", "aux": "aux"}
POS_FULL_RE = re.compile(r"^\s*(n|vt|vi|v|adj|a|adv|conj|prep|pron|num|art|int|aux)\s*[.．]",
                         re.I)


def part_of_speech(text):
    """取词性标记：'n. 侍者' -> 'n'，'vt. 翻译' -> 'v'。"""
    match = POS_FULL_RE.match(text or "")
    return POS_SHORT.get(match.group(1).lower()) if match else None


def _weigh_into(terms, gloss):
    """把一条释义的义项（和单字兜底）并进 terms；同一个词条取较大权重。"""
    for idx, term in enumerate(gloss_terms(gloss)):
        weight = min(len(term), 4) * 1.0 / (1.0 + 0.25 * idx)
        if weight > terms.get(term, 0.0):
            terms[term] = weight
        for ch in term:
            if ch in CHAR_STOP:
                continue
            if 0.5 > terms.get(ch, 0.0):
                terms[ch] = 0.5


def terms_from_gloss(gloss):
    """一条中文释义 -> [(词条, 权重)]，越靠前的义项权重越高。"""
    terms = {}
    _weigh_into(terms, gloss)
    return sorted(terms.items(), key=lambda kv: -kv[1])


def stem_terms(sentence, dictionary=None):
    """英文题干 -> [(中文词条, 权重)]，虚词跳过、查不到的词跳过。

    释义里越靠前的义项越常用，给个由前到后的衰减。
    另外把每个词条的字也单独加一份（权重低）：词表常写"小刀"，选项只写"刀"，
    不做单字兜底就会漏判。
    """
    dictionary = dictionary or get_dict()
    tokens = [t.lower() for t in TOKEN_RE.findall(sentence)]
    # OCR 有时会把一个词拆开（transport -> "tra nsport"），粘回去再查
    if len(tokens) >= 2 and any(not dictionary.form(t) for t in tokens):
        joined = "".join(tokens)
        if dictionary.form(joined):
            tokens = [joined]

    def collect(allow_stop):
        terms = {}
        for word in tokens:
            if not allow_stop and (word in STOPWORDS or len(word) < 2):
                continue
            gloss = dictionary.form(word)
            if not gloss:
                continue
            _weigh_into(terms, gloss)
        return terms

    terms = collect(False)
    if not terms:
        # 题干全是虚词（all / also / a 这类）：虚词也得查，不然返回空只能靠蒙
        terms = collect(True)
    return sorted(terms.items(), key=lambda kv: -kv[1])


def clean_option(text):
    """去掉选项前面的 "A." "B、" 之类标记。"""
    return re.sub(r"^\s*[A-Da-d]\s*[.、．:：)）]\s*", "", text).strip()


def _idf_weights(terms, options):
    """选项里人人都有的词条（比如都提到"她"）没有区分度，降权。"""
    n = max(1, len(options))
    weights = []
    for term, weight in terms:
        df = sum(1 for opt in options if term in opt)
        weights.append((term, weight * (1.0 - 0.85 * (df - 1) / n)))
    return weights


def _rank_with_terms(option_texts, terms):
    options = [clean_option(t) for t in option_texts]
    if not terms or not options:
        return [0.0] * len(options)

    weights = _idf_weights(terms, options)
    total = sum(w for _, w in weights) or 1.0
    scores = []
    for opt in options:
        hit = sum(w for term, w in weights if term and term in opt)
        scores.append(hit / total)
    return scores


def rank_options(option_texts, stem):
    """给每个选项打 0~1 的分，返回列表（和入参同序）。"""
    return _rank_with_terms(option_texts, stem_terms(stem))


def rank_gloss(option_texts, gloss):
    """用一条现成的中文释义给选项打分（不查词表）。

    单词卡背面就是 app 自己的释义，比 ECDICT 一整串义项更贴近出题口径。
    """
    return _rank_with_terms(option_texts, terms_from_gloss(gloss))


def explain(option_texts, stem):
    """调试用：打印题干被拆成了哪些中文词条、每个选项命中哪些。"""
    terms = stem_terms(stem)
    lines = ["题干词条: " + ("、".join(t for t, _ in terms) if terms else "（一个都没查到！）")]
    for i, (text, score) in enumerate(zip(option_texts, rank_options(option_texts, stem))):
        opt = clean_option(text)
        hit = [t for t, _ in terms if t in opt]
        lines.append("  选项%d %.2f  命中: %s" % (i + 1, score, "、".join(hit) or "-"))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    dic = get_dict()
    print("词表: %s  词条数: %d" % (dic.path, len(dic)))
    words = sys.argv[1:] or ["abandon", "accused", "stealing", "ubiquitous", "serendipity"]
    for w in words:
        print("  %-14s -> %s" % (w, dic.form(w) or "（未收录）"))
    if len(words) >= 2:
        demo = ["A. 放弃；抛弃", "B. 吸收；专注", "C. 适应；改编", "D. 钦佩；赞赏"]
        print(explain(demo, words[0]))
