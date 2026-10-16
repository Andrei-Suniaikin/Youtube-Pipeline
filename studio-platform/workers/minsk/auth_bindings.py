"""План и применение привязок «профиль+канал → почта» для автовхода заливщика.

Реестр (`upload_accounts_recovery.json`) лежит на Минске, 0600, с паролями — сюда не
попадает. Скрипт работает на Минске: читает реестр и CSV «профиль,канал,почта»,
печатает ТОЛЬКО счётчики и серилы/UC, почты и пароли не выводит.

Правила (те же, что у owner_fallback.merge_owner_alias): только добавление; уже
существующую привязку не заменять и не дублировать; конфликт — пропуск с причиной;
нужна запись в `accounts` с паролем и totp; профиль в привязке — строка.

    python3 auth_bindings.py REGISTRY.json PAIRS.csv            # сухой прогон
    python3 auth_bindings.py REGISTRY.json PAIRS.csv --apply    # бэкап + атомарная запись
"""
import argparse, copy, csv, json, os, shutil, sys, tempfile, time
from collections import Counter


def plan(document, pairs):
    """Возвращает (новый документ, итоги). pairs: [(profile, uc, email)]."""
    accounts = {str(a.get("login") or "").strip().lower(): a for a in document.get("accounts", [])}
    existing = {}
    for b in document.get("bindings", []):
        existing.setdefault((str(b.get("profile")), b.get("channel_uc")), []).append(
            str(b.get("login") or "").strip().lower())
    result = copy.deepcopy(document)
    out = {"add": [], "skip": []}
    seen = set()
    for profile, uc, email in pairs:
        profile, uc, login = str(profile).strip(), str(uc).strip(), str(email or "").strip().lower()
        key = (profile, uc)
        if key in seen:
            out["skip"].append((key, "duplicate_in_input")); continue
        seen.add(key)
        if not login:
            out["skip"].append((key, "no_email")); continue
        acc = accounts.get(login)
        if acc is None:
            out["skip"].append((key, "no_account_in_registry")); continue
        if not acc.get("password") or not acc.get("totp"):
            out["skip"].append((key, "account_without_password_or_totp")); continue
        have = existing.get(key, [])
        if len(have) > 1:
            out["skip"].append((key, "ambiguous_existing")); continue
        if have:
            out["skip"].append((key, "already_bound" if have[0] == login else "conflicts_with_existing")); continue
        result["bindings"].append({"profile": profile, "channel_uc": uc, "login": acc["login"]})
        out["add"].append(key)
    return result, out


def read_pairs(path):
    with open(path, newline="", encoding="utf-8") as f:
        return [(r[0], r[1], r[2] if len(r) > 2 else "") for r in csv.reader(f)
                if r and r[0].strip() and not r[0].lstrip().startswith("#")]


def apply(path, document):
    st = os.stat(path)
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-bindings"
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".bindings-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(document, f, ensure_ascii=False, indent=1)
        f.flush(); os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    if os.stat(path).st_mtime_ns != st.st_mtime_ns:      # параллельная правка (другой чат/Володя)
        os.unlink(tmp); raise SystemExit("реестр изменился во время работы — повторите")
    os.replace(tmp, path)
    return backup


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("registry"); ap.add_argument("pairs"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    document = json.load(open(a.registry, encoding="utf-8"))
    new, out = plan(document, read_pairs(a.pairs))
    print("добавить:", len(out["add"]), "| пропуск:", dict(Counter(r for _, r in out["skip"])))
    for (p, uc) in out["add"]:
        print("  +", p, uc[:10])
    if a.apply and out["add"]:
        print("бэкап:", apply(a.registry, new))
    elif out["add"]:
        print("сухой прогон: --apply не указан, реестр не тронут")
    return 0


if __name__ == "__main__":
    sys.exit(main())
