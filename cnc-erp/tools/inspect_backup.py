#!/usr/bin/env python3
"""檢查舊系統（例如凌越）備份檔的格式與內容結構。

用法：python tools/inspect_backup.py wstkBKUP.001
      python tools/inspect_backup.py wstkBKUP.001 --extract 輸出資料夾   （能解開的話，順便解出來）

預設只列出「格式、資料表名稱、欄位名稱、筆數」，不會印出客戶資料內容，
可以放心把結果貼給別人看。加 --sample 才會印出每個資料表前 3 筆。
"""
import argparse
import collections
import gzip
import io
import math
import os
import re
import struct
import zlib
import sys
import tarfile
import zipfile

SIGNATURES = [
    (b"PK\x03\x04", "ZIP 壓縮檔"),
    (b"Rar!\x1a\x07", "RAR 壓縮檔"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip 壓縮檔"),
    (b"MSCF", "Microsoft CAB 壓縮檔"),
    (b"\x1f\x8b", "GZIP 壓縮檔"),
    (b"\x60\xea", "ARJ 壓縮檔"),
    (b"BZh", "BZIP2 壓縮檔"),
    (b"\x00\x01\x00\x00Standard Jet DB", "Microsoft Access 資料庫 (.mdb)"),
    (b"\x00\x01\x00\x00Standard ACE DB", "Microsoft Access 資料庫 (.accdb)"),
    (b"TAPE", "Microsoft SQL Server 備份 (.bak)"),
    (b"SQLite format 3\x00", "SQLite 資料庫"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "Microsoft OLE 複合文件（舊版 Office / 部分資料庫）"),
    (b"MSWIM\x00", "Windows 映像檔 (WIM)"),
    (b"\x78\x9c", "zlib 壓縮資料"),
]
# 檔案「中間」也可能藏著這些東西（自訂備份格式常把多個檔案串在一起）
INNER_SIGNATURES = [
    (b"PK\x03\x04", "ZIP 檔頭"),
    (b"Standard Jet DB", "Access 資料庫"),
    (b"Standard ACE DB", "Access 資料庫"),
    (b"SQLite format 3\x00", "SQLite 資料庫"),
    (b"Rar!\x1a\x07", "RAR 檔頭"),
    (b"MSCF\x00\x00\x00\x00", "CAB 檔頭"),
]
FILENAME_RE = re.compile(rb"[A-Za-z0-9_\-\\/:.~$]{3,60}\.(?:DBF|dbf|Dbf|MDB|mdb|ACCDB|accdb|DAT|dat|FPT|fpt|CDX|cdx|IDX|idx|NTX|ntx|MDF|mdf|LDF|ldf|DB|db|INI|ini|TXT|txt|BTR|btr)(?![A-Za-z0-9])")
DBF_CANDIDATE_RE = re.compile(rb"(?=[\x02\x03\x30\x31\x32\x43\x63\x83\x8b\x8e\xcb\xf5\xfb][\x00-\xff][\x01-\x0c][\x01-\x1f])")
DBF_VERSIONS = {0x02, 0x03, 0x30, 0x31, 0x32, 0x43, 0x63, 0x83, 0x8B, 0x8E, 0xCB, 0xF5, 0xFB}


def hexdump(b):
    return " ".join("%02x" % c for c in b)


def entropy(b):
    """每個位元組的資訊量（0~8）。7.9 以上通常代表資料被壓縮或加密。"""
    if not b:
        return 0.0
    counts = collections.Counter(b)
    n = len(b)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def describe_unknown(buf, indent):
    """格式不明時，盡量多給線索（不含客戶資料內容）。"""
    print(f"{indent}  開頭 64 位元組：{hexdump(buf[:64])}")
    printable = "".join(chr(c) if 32 <= c < 127 else "." for c in buf[:64])
    print(f"{indent}  開頭可讀字元：{printable}")
    if len(buf) > 2 and buf[2:4] == b"-l" and buf[6:7] == b"-":
        print(f"{indent}  ➜ 看起來是 LZH 壓縮檔（{buf[2:7].decode('ascii', 'replace')}）")
    chunks = [buf[i:i + 65536] for i in range(0, len(buf), max(len(buf) // 8, 65536))][:8]
    print(f"{indent}  資料亂度（0~8，越接近 8 越像壓縮或加密）：" + " ".join("%.2f" % entropy(c) for c in chunks))
    for sig, label in INNER_SIGNATURES:
        hits = [m.start() for m in re.finditer(re.escape(sig), buf)][:5]
        if hits:
            total = buf.count(sig)
            print(f"{indent}  內含 {label} {total} 處，位置：{', '.join(map(str, hits))}{' …' if total > 5 else ''}")
    names = collections.Counter(m.group().decode("latin-1") for m in FILENAME_RE.finditer(buf))
    if names:
        print(f"{indent}  檔案裡出現的檔名（共 {len(names)} 種，列前 60 個）：")
        for name, cnt in names.most_common(60):
            print(f"{indent}    {name}  ×{cnt}")
    else:
        print(f"{indent}  檔案裡沒有看到 .DBF / .MDB 之類的檔名")


def decode(b):
    for enc in ("cp950", "big5hkscs", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("cp950", errors="replace")


def parse_dbf_header(buf, off=0):
    """若 buf[off:] 看起來是 DBF 檔頭，回傳 (欄位清單, 筆數, 檔頭長, 每筆長度)，否則 None。"""
    if off + 32 > len(buf) or buf[off] not in DBF_VERSIONS:
        return None
    yy, mm, dd = buf[off + 1:off + 4]
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    n_rec, hdr_len, rec_len = struct.unpack("<IHH", buf[off + 4:off + 12])
    if hdr_len < 65 or rec_len < 2 or n_rec > 50_000_000:
        return None
    fields, pos = [], off + 32
    while pos + 32 <= len(buf) and buf[pos] != 0x0D:
        raw_name = buf[pos:pos + 11].split(b"\0")[0]
        ftype = chr(buf[pos + 11])
        if not raw_name or ftype not in "CNFDLMIBGPTYV0@+O":
            return None
        fields.append((decode(raw_name).strip(), ftype, buf[pos + 16]))
        pos += 32
        if len(fields) > 255:
            return None
    if not fields or 1 + sum(f[2] for f in fields) != rec_len:
        return None
    return fields, n_rec, hdr_len, rec_len


def dbf_rows(buf, off, info, limit):
    fields, n_rec, hdr_len, rec_len = info
    out = []
    for i in range(min(n_rec, limit)):
        rec = buf[off + hdr_len + i * rec_len: off + hdr_len + (i + 1) * rec_len]
        p, row = 1, []
        for _, t, ln in fields:
            row.append(decode(rec[p:p + ln]).strip() if t not in "MGPB" else "<備忘>")
            p += ln
        out.append(row)
    return out


def report_dbf(name, buf, off, info, sample):
    fields, n_rec, _, _ = info
    print(f"  ▸ 資料表 {name}：{n_rec} 筆，{len(fields)} 個欄位")
    print("    欄位：" + "、".join(f"{f[0]}({f[1]}{f[2]})" for f in fields))
    if sample:
        for r in dbf_rows(buf, off, info, 3):
            print("    範例：" + " | ".join(r))


def scan_embedded(buf, sample, extract_dir=None):
    """格式不明時，在整個檔案裡找藏在裡面的 DBF 或 ZIP。"""
    found = 0
    next_free = 0
    for m in DBF_CANDIDATE_RE.finditer(buf):
        i = m.start()
        if i < next_free:
            continue
        info = parse_dbf_header(buf, i)
        if info:
            found += 1
            fields, n_rec, hdr_len, rec_len = info
            end = i + hdr_len + n_rec * rec_len
            # 往前找看看有沒有檔名（很多備份格式會把檔名放在資料前面）
            before = buf[max(0, i - 128):i]
            names = [m.group().decode("latin-1") for m in FILENAME_RE.finditer(before)
                     if m.group().upper().endswith(b".DBF")]
            name_guess = names[-1] if names else ""
            report_dbf(name_guess or f"#{found}（位移 {i}）", buf, i, info, sample)
            if extract_dir:
                fn = re.split(r"[\\/:]", name_guess)[-1] if name_guess.upper().endswith(".DBF") else f"table_{found}.dbf"
                with open(os.path.join(extract_dir, fn), "wb") as f:
                    f.write(buf[i:end])
            next_free = end
    return found


def try_zlib_streams(buf, sample, extract_dir, indent):
    """自訂備份格式常用 zlib 壓縮。找出檔案裡的 zlib 區塊，解開後再找資料表。"""
    found_streams = found_tables = 0
    pos_list = [m.start() for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", buf)]
    next_free = 0
    for pos in pos_list:
        if pos < next_free:
            continue
        d = zlib.decompressobj()
        try:
            out = d.decompress(buf[pos:pos + 200_000_000], 300_000_000)
        except zlib.error:
            continue
        if len(out) < 256:
            continue
        found_streams += 1
        used = len(buf) - pos - len(d.unused_data) if d.eof else len(buf) - pos
        next_free = pos + max(used, 2)
        print(f"{indent}  ▸ zlib 壓縮區塊 #{found_streams}：位置 {pos}，解開後 {len(out):,} bytes"
              f"，開頭 {''.join(chr(c) if 32 <= c < 127 else '.' for c in out[:24])}")
        if parse_dbf_header(out):
            found_tables += 1
            report_dbf(f"區塊 #{found_streams}", out, 0, parse_dbf_header(out), sample)
            if extract_dir:
                with open(os.path.join(extract_dir, f"block_{found_streams}.dbf"), "wb") as f:
                    f.write(out)
        else:
            found_tables += scan_embedded(out, sample, extract_dir)
        if extract_dir:
            with open(os.path.join(extract_dir, f"block_{found_streams}.bin"), "wb") as f:
                f.write(out)
    return found_streams, found_tables


def inspect_bytes(name, buf, sample, extract_dir, depth=0):
    indent = "  " * depth
    kind = next((label for sig, label in SIGNATURES if buf.startswith(sig)), None)
    info = parse_dbf_header(buf)
    if info:
        print(f"{indent}{name}：dBASE / FoxPro 資料表（新系統可直接匯入）")
        report_dbf(name, buf, 0, info, sample)
        return
    if kind is None:
        print(f"{indent}{name}：不是常見格式")
        describe_unknown(buf, indent)
        print(f"{indent}  正在掃描檔案內部是否包含資料表…")
        n = scan_embedded(buf, sample, extract_dir)
        print(f"{indent}  正在嘗試解開檔案內的壓縮區塊…")
        streams, tables = try_zlib_streams(buf, sample, extract_dir, indent)
        n += tables
        if n:
            print(f"{indent}  ➜ 共找到 {n} 個資料表")
        elif streams:
            print(f"{indent}  解開了 {streams} 個壓縮區塊，但裡面不是 DBF 資料表；請把這份結果回傳。")
        else:
            print(f"{indent}  沒找到資料表或可解開的壓縮區塊；可能是加密或其他壓縮格式，請把這份結果回傳。")
        return
    print(f"{indent}{name}：{kind}")
    if kind.startswith("ZIP"):
        with zipfile.ZipFile(io.BytesIO(buf)) as z:
            for zi in z.infolist():
                print(f"{indent}  - {zi.filename}（{zi.file_size:,} bytes）")
            for zi in z.infolist():
                if zi.is_dir() or zi.flag_bits & 0x1:
                    continue
                data = z.read(zi)
                if extract_dir:
                    target = os.path.join(extract_dir, os.path.basename(zi.filename))
                    with open(target, "wb") as f:
                        f.write(data)
                inspect_bytes(zi.filename, data, sample, None, depth + 1)
            if any(zi.flag_bits & 0x1 for zi in z.infolist()):
                print(f"{indent}  ⚠️ 有檔案設了密碼，需要密碼才能讀")
    elif kind.startswith("GZIP"):
        data = gzip.decompress(buf)
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                for m in t.getmembers():
                    if m.isfile():
                        inspect_bytes(m.name, t.extractfile(m).read(), sample, extract_dir, depth + 1)
        except tarfile.TarError:
            inspect_bytes(name + "（解壓後）", data, sample, extract_dir, depth + 1)
    else:
        describe_unknown(buf, indent)
        print(f"{indent}  這個格式需要另外的工具解開，請把這份結果回傳，我再處理。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="備份檔（若有 .001 .002 … 分割檔，請全部列出，依序）")
    ap.add_argument("--sample", action="store_true", help="印出每個資料表前 3 筆（會包含資料內容）")
    ap.add_argument("--extract", metavar="資料夾", help="把找到的資料表另存成 .dbf 等檔案")
    args = ap.parse_args()
    if args.extract:
        os.makedirs(args.extract, exist_ok=True)
    buf = b"".join(open(f, "rb").read() for f in args.files)
    print(f"檔案：{', '.join(os.path.basename(f) for f in args.files)}，共 {len(buf):,} bytes")
    inspect_bytes(os.path.basename(args.files[0]), buf, args.sample, args.extract)
    if args.extract:
        print(f"\n已解出到：{os.path.abspath(args.extract)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write("\ufeff")  # 讓舊版記事本認得 UTF-8，不會變亂碼
    print(f"Python {sys.version.split()[0]}")
    main()
