# -*- coding: utf-8 -*-
"""下载 ECDICT 英汉词典并压成项目用的紧凑词表（一次性构建脚本，可删）。

产物：词典.tsv.gz   每行 "单词<TAB>中文释义"
"""
import csv
import gzip
import io
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "_ecdict_raw.csv")
OUT = os.path.join(HERE, "词典.tsv.gz")
TMP = OUT + ".tmp"
RAW_MIN_BYTES = 40 * 1024 * 1024     # 完整文件约 60MB，小于这个说明没下完
MIN_ENTRIES = 200000                 # 太少说明解析出了问题，不要覆盖好词表
URLS = [
    "https://gh-proxy.com/https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv",
    "https://gh-proxy.com/raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv",
]


def download():
    if os.path.isfile(RAW) and os.path.getsize(RAW) > RAW_MIN_BYTES:
        print("已有完整原始文件，跳过下载: %s" % RAW)
        return
    if os.path.isfile(RAW):
        print("原始文件不完整（%.1f MB），重新下载"
              % (os.path.getsize(RAW) / 1048576))
    for url in URLS:
        for attempt in range(3):
            try:
                print("下载 %s (第%d次)" % (url, attempt + 1))
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    total, got, t0 = 0, 0, time.time()
                    with open(RAW, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                            got += len(chunk)
                            if got // (5 << 20) != total // (5 << 20):
                                print("  %.0f MB  %.0f KB/s" % (got / 1048576,
                                                                got / 1024 / (time.time() - t0)))
                            total = got
                with open(RAW, "rb") as f:
                    head = f.readline()[:200]
                if b"word" in head and b"translation" in head:
                    print("下载完成 %.1f MB" % (os.path.getsize(RAW) / 1048576))
                    return
                print("  文件头不对，重试")
            except Exception as exc:
                print("  失败: %s" % exc)
                time.sleep(2)
    raise SystemExit("下载失败")


def has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def locate_source():
    """找原始 CSV：命令行参数 > 项目里的 _ecdict_raw.csv > 解压出来的 ECDICT 目录。"""
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isfile(path):
            return path
        raise SystemExit("找不到文件: %s" % path)
    if os.path.isfile(RAW) and os.path.getsize(RAW) > RAW_MIN_BYTES:
        return RAW
    for root, _dirs, files in os.walk(HERE):
        for name in files:
            if name.lower() in ("ecdict.csv", "ecdict_full.csv"):
                path = os.path.join(root, name)
                if os.path.getsize(path) > RAW_MIN_BYTES:
                    return path
    return None


def build(source):
    """先写到 .tmp，条数够多才替换正式词表 —— 中途被打断不会弄坏现成的 词典.tsv.gz。"""
    kept = 0
    print("源文件: %s (%.1f MB)" % (source, os.path.getsize(source) / 1048576))
    with open(source, "r", encoding="utf-8", newline="") as fin, \
            gzip.open(TMP, "wt", encoding="utf-8", compresslevel=9) as fout:
        reader = csv.DictReader(fin)
        fields = reader.fieldnames or []
        print("列:", fields)
        for row in reader:
            word = (row.get("word") or "").strip()
            trans = (row.get("translation") or "").replace("\\n", "；")
            trans = "；".join(p.strip() for p in trans.replace("\n", "；").split("；") if p.strip())
            if not word or not trans or not has_cjk(trans):
                continue
            # 统一转小写：Tokyo / Africa 这类专有名词也要收，否则单词卡里常见的地名全查不到
            key = word.strip().lower()
            if not key or " " in key or any(ch.isdigit() for ch in key):
                continue
            fout.write("%s\t%s\n" % (key, trans))
            kept += 1
            if kept % 100000 == 0:
                print("  %d 条" % kept)
    size = os.path.getsize(TMP) / 1048576
    if kept < MIN_ENTRIES:
        os.remove(TMP)
        raise SystemExit("只解析出 %d 条，太少，保留原词表不动" % kept)
    os.replace(TMP, OUT)
    print("写入 %d 条 -> %s (%.1f MB)" % (kept, OUT, size))


if __name__ == "__main__":
    src = locate_source()
    if src is None:
        download()
        src = RAW
    build(src)
