#!/usr/bin/env python3
"""
Termux Package Installer v2 — edisi cepat.

Satu file, tanpa dependensi di luar standard library.
Dua mode pemakaian:
  * Interaktif : python3 install.py            (menu, ramah pemula)
  * CLI cepat  : python3 install.py --all      (tanpa prompt, cocok script)

Yang baru dibanding v1:
  * Instalasi sistem di-batch (satu panggilan pkg untuk banyak package),
    bukan satu-satu seperti v1 — jauh lebih sedikit overhead proses.
  * Package pip dipasang paralel dengan thread pool (--jobs).
  * Package yang sudah terinstal dilewati (snapshot dpkg/pip sekali di awal).
  * Kegagalan per kelompok dipecah otomatis: batch yang gagal dicoba ulang
    satu per satu supaya hanya package bermasalah yang dilaporkan gagal.
  * Ringkasan akhir berisi durasi + daftar gagal, dan semua aktivitas
    dicatat ke install.log.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent
PACKAGES_FILE = BASE_DIR / "packages.txt"
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"
LOG_FILE = BASE_DIR / "install.log"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._\-\[\]]*$")

USE_COLOR = sys.stdout.isatty()


def c(code: str) -> str:
    return code if USE_COLOR else ""


RESET = c("\033[0m")
BOLD = c("\033[1m")
DIM = c("\033[90m")
RED = c("\033[91m")
GREEN = c("\033[92m")
YELLOW = c("\033[93m")
BLUE = c("\033[94m")
CYAN = c("\033[96m")


def log_line(text: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def say(text: str = "") -> None:
    print(text, flush=True)
    log_line(re.sub(r"\033\[[0-9;]*m", "", text))


# ─── Banner ────────────────────────────────────────────────────────────────

def banner() -> None:
    try:
        out = subprocess.run(
            ["toilet", "-f", "standard", "SoraaaID"],
            capture_output=True, text=True, timeout=5,
        )
        art = out.stdout.rstrip("\n") if out.returncode == 0 else ""
    except Exception:
        art = ""
    if not art:
        art = (
            "  ____                              ___ ____\n"
            " / ___|  ___  _ __ __ _  __ _  __ _|_ _|  _ \\\n"
            " \\___ \\ / _ \\| '__/ _` |/ _` |/ _` || || | | |\n"
            "  ___) | (_) | | | (_| | (_| | (_| || || |_| |\n"
            " |____/ \\___/|_|  \\__,_|\\__,_|\\__,_|___|____/"
        )
    say(f"{CYAN}{BOLD}{art}{RESET}")
    say(f"{BLUE}{'=' * 62}{RESET}")
    say(f" {BOLD}Author     :{RESET} SoraaaID")
    say(f" {BOLD}Deskripsi  :{RESET} Tools Instalasi Package Termux Buat Pemula — v{VERSION} (cepat)")
    say(f" {BOLD}Repository :{RESET} https://github.com/SoraaaID/Termux-Package")
    say(f"{BLUE}{'=' * 62}{RESET}")


# ─── Data ──────────────────────────────────────────────────────────────────

def load_list(path: Path) -> list[str]:
    if not path.exists():
        say(f"{YELLOW}[!] File {path.name} tidak ditemukan, dilewati.{RESET}")
        return []
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        item = raw.strip()
        if item and not item.startswith("#"):
            names.append(item)
    return names


def valid_names(names: list[str]) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for n in names:
        (ok if NAME_RE.match(n) else bad).append(n)
    for n in bad:
        say(f"{YELLOW}[!] Nama package tidak valid, dilewati: {n!r}{RESET}")
    return ok, bad


# ─── Snapshot status terinstal ─────────────────────────────────────────────

def installed_system() -> set[str]:
    """Satu snapshot dpkg untuk semua package sistem (murah, sekali jalan)."""
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\n"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    return set()


def installed_pip() -> set[str]:
    """Satu snapshot pip list (nama dinormalisasi: lowercase, _ -> -)."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0:
            names = set()
            for line in out.stdout.splitlines():
                if "==" in line:
                    names.add(line.split("==", 1)[0].strip().lower().replace("_", "-"))
            return names
    except Exception:
        pass
    return set()


def pip_base(name: str) -> str:
    return re.split(r"[\[<>=!~;\s]", name, maxsplit=1)[0].lower().replace("_", "-")


# ─── Eksekusi ──────────────────────────────────────────────────────────────

@dataclass
class Result:
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    seconds: float = 0.0


def run_quiet(cmd: list[str], timeout: int = 1800) -> int:
    try:
        done = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True, timeout=timeout)
        if done.returncode != 0 and done.stderr:
            log_line(f"[stderr] {' '.join(cmd)}\n{done.stderr.strip()[:800]}")
        return done.returncode
    except Exception as exc:  # noqa: BLE001 - dilaporkan sebagai gagal, bukan crash
        log_line(f"[error] {' '.join(cmd)} -> {exc}")
        return 1


def pkg_manager() -> str:
    for cand in ("pkg", "apt-get", "apt"):
        if shutil.which(cand):
            return cand
    return "pkg"


def install_system(names: list[str], *, dry: bool, chunk: int = 10) -> Result:
    """Batch install: beberapa package per panggilan pkg; batch gagal dipecah."""
    res = Result()
    t0 = time.monotonic()
    mgr = pkg_manager()
    have = installed_system()
    todo = []
    for n in names:
        if n in have:
            res.skipped.append(n)
        else:
            todo.append(n)
    if res.skipped:
        say(f"{DIM}  Dilewati (sudah terinstal): {', '.join(res.skipped)}{RESET}")
    groups = [todo[i:i + chunk] for i in range(0, len(todo), chunk)]
    for gi, group in enumerate(groups, 1):
        label = ", ".join(group)
        if dry:
            say(f"  [dry-run] {mgr} install -y {label}")
            res.installed.extend(group)
            continue
        say(f"  [{gi}/{len(groups)}] Batch: {label} ...", )
        if run_quiet([mgr, "install", "-y", *group]) == 0:
            res.installed.extend(group)
            say(f"        {GREEN}✓ OK{RESET}")
        else:
            # Batch gagal → coba satu-satu agar hanya yang bermasalah gagal
            say(f"        {YELLOW}Batch gagal, dicoba satu per satu...{RESET}")
            for n in group:
                if run_quiet([mgr, "install", "-y", n]) == 0:
                    res.installed.append(n)
                    say(f"        {GREEN}✓{RESET} {n}")
                else:
                    res.failed.append(n)
                    say(f"        {RED}✗{RESET} {n}")
    res.seconds = time.monotonic() - t0
    return res


def _pip_one(name: str, dry: bool) -> tuple[str, bool]:
    if dry:
        return name, True
    rc = run_quiet([sys.executable, "-m", "pip", "install", "--quiet", name], timeout=900)
    if rc != 0:  # satu kali retry, kadang kegagalan jaringan sesaat
        rc = run_quiet([sys.executable, "-m", "pip", "install", "--quiet", name], timeout=900)
    return name, rc == 0


def install_pip(names: list[str], *, dry: bool, jobs: int) -> Result:
    """Pip install paralel dengan thread pool."""
    res = Result()
    t0 = time.monotonic()
    have = installed_pip()
    todo = []
    for n in names:
        if pip_base(n) in have:
            res.skipped.append(n)
        else:
            todo.append(n)
    if res.skipped:
        say(f"{DIM}  Dilewati (sudah terinstal): {', '.join(res.skipped)}{RESET}")
    if dry:
        for n in todo:
            say(f"  [dry-run] pip install {n}")
        res.installed.extend(todo)
    elif todo:
        workers = max(1, min(jobs, len(todo)))
        say(f"  Memasang {len(todo)} package pip dengan {workers} worker paralel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for name, ok in pool.map(lambda n: _pip_one(n, dry), todo):
                if ok:
                    res.installed.append(name)
                    say(f"        {GREEN}✓{RESET} {name}")
                else:
                    res.failed.append(name)
                    say(f"        {RED}✗{RESET} {name}")
    res.seconds = time.monotonic() - t0
    return res


def update_index(dry: bool) -> None:
    mgr = pkg_manager()
    if dry:
        say(f"  [dry-run] {mgr} update -y")
        return
    say(f"  Menjalankan {mgr} update ...")
    run_quiet([mgr, "update", "-y"], timeout=1200)


# ─── Orkestrasi ────────────────────────────────────────────────────────────

def summary(tag: str, res: Result) -> None:
    say(f"{CYAN}Ringkasan {tag} ({res.seconds:.1f} detik):{RESET}")
    say(f"  • Terinstal : {GREEN}{len(res.installed)}{RESET}")
    say(f"  • Dilewati  : {DIM}{len(res.skipped)}{RESET} (sudah ada)")
    if res.failed:
        say(f"  • Gagal     : {RED}{len(res.failed)}{RESET} ({', '.join(res.failed)})")
    else:
        say(f"  • Gagal     : {GREEN}0{RESET}")


def do_update(dry: bool) -> None:
    say(f"\n{BOLD}➜ Update index repository{RESET}")
    update_index(dry)


def do_system(dry: bool) -> Result:
    names, _ = valid_names(load_list(PACKAGES_FILE))
    say(f"\n{BOLD}➜ Package sistem ({len(names)} item dari {PACKAGES_FILE.name}){RESET}")
    res = install_system(names, dry=dry)
    summary("package sistem", res)
    return res


def do_python(dry: bool, jobs: int) -> Result:
    names, _ = valid_names(load_list(REQUIREMENTS_FILE))
    say(f"\n{BOLD}➜ Package Python/pip ({len(names)} item dari {REQUIREMENTS_FILE.name}){RESET}")
    res = install_pip(names, dry=dry, jobs=jobs)
    summary("package Python", res)
    return res


def do_all(dry: bool, jobs: int) -> None:
    do_update(dry)
    do_system(dry)
    do_python(dry, jobs)


MENU = (
    ("\n{B}PILIHAN MENU:{R}\n"
     " {C}[1]{R} Install Python & Pip Packages (requirements.txt) — paralel\n"
     " {C}[2]{R} Install Semua Package (Update + Sistem + Python) — mode cepat\n"
     " {C}[3]{R} Tampilkan Banner Saja\n"
     " {C}[4]{R} Install Package Sistem Saja (packages.txt) — batch\n"
     " {RED}[0]{R} Keluar\n")
)


def interactive(jobs: int) -> int:
    banner()
    while True:
        say(MENU.format(B=BOLD, R=RESET, C=CYAN, RED=RED))
        try:
            choice = input(f"{YELLOW}Pilih opsi [0-4]: {RESET}").strip()
        except EOFError:
            say("")
            return 0
        if choice == "0":
            say("Sampai jumpa! 👋")
            return 0
        if choice == "1":
            do_python(dry=False, jobs=jobs)
        elif choice == "2":
            do_all(dry=False, jobs=jobs)
        elif choice == "3":
            banner()
        elif choice == "4":
            do_system(dry=False)
        else:
            say(f"{RED}[!] Pilihan tidak dikenal: {choice!r}{RESET}")
            continue
        try:
            input(f"\n{DIM}Tekan [Enter] untuk kembali ke menu...{RESET}")
        except EOFError:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Termux Package Installer v2 — batch + paralel, jauh lebih cepat dari v1.",
    )
    parser.add_argument("--all", action="store_true", help="Update + sistem + Python, tanpa prompt")
    parser.add_argument("--system", action="store_true", help="Hanya package sistem (packages.txt)")
    parser.add_argument("--python", action="store_true", help="Hanya package pip (requirements.txt)")
    parser.add_argument("--update", action="store_true", help="Hanya update index repository")
    parser.add_argument("--banner", action="store_true", help="Tampilkan banner saja")
    parser.add_argument("--jobs", type=int, default=4, help="Worker paralel untuk pip (default: 4)")
    parser.add_argument("--dry-run", action="store_true", help="Tampilkan rencana tanpa memasang apa pun")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)

    if not (args.all or args.system or args.python or args.update or args.banner):
        return interactive(jobs=args.jobs)

    banner()
    failed_total = 0
    if args.banner:
        return 0
    if args.update or args.all:
        do_update(args.dry_run)
    if args.system or args.all:
        failed_total += len(do_system(args.dry_run).failed)
    if args.python or args.all:
        failed_total += len(do_python(args.dry_run, args.jobs).failed)
    say(f"\n{GREEN if failed_total == 0 else YELLOW}Selesai. Log tersimpan di {LOG_FILE.name}{RESET}")
    return 0 if failed_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
