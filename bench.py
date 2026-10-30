"""Замер: как размер картинки меняет ответы и скорость. Берёт уже разобранные скриншоты (поровну из каждой темы),
разбирает их заново при разных размерах и сравнивает тему с прошлым ответом.
Запуск: python bench.py --n 60 --pixels 1080x2160,900x1800,736x1472
Ответы — в _данные/bench/<модель>-<размер>.jsonl, чтобы посмотреть расхождения глазами."""
import argparse, json, random, time
from collections import defaultdict

import sortshots as S


def sample(n, seed=1):
    res, by_topic = S.load_results(), defaultdict(list)
    for sha, r in res.items():
        p = S.SRC / r["src"]
        if r.get("fast") and p.exists():
            by_topic[r["fast"]["topic"]].append((sha, p, r["fast"]))
    rnd, out = random.Random(seed), []
    for items in by_topic.values():
        rnd.shuffle(items)
    while len(out) < n and any(by_topic.values()):  # по одному из каждой темы по кругу: редкие темы тоже попадут
        for t in sorted(by_topic):
            if by_topic[t] and len(out) < n:
                out.append(by_topic[t].pop())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--pixels", default="1080x2160,900x1800,736x1472")
    ap.add_argument("--model", default=S.FAST)
    a = ap.parse_args()
    S.ensure_ollama()
    shots, topic_list = sample(a.n), S.topics()
    out_dir = S.DATA / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(shots)} скриншотов, тем {len({f['topic'] for *_, f in shots})}")
    S.ask(a.model, shots[0][1], topic_list)  # прогрев: загрузка модели не должна попасть в замер
    report, ctx = [], S.NUM_CTX
    for size in a.pixels.split(","):
        w, h = map(int, size.split("x"))
        S.NUM_CTX = 6144 if w * h > 1.5e6 else ctx  # полный размер — со старым окном, как было до уменьшения
        same, secs, toks, t0 = 0, [], [], time.time()
        with open(out_dir / f"{a.model.replace(':', '-')}-{size}.jsonl", "w", encoding="utf-8") as f:
            for sha, p, old in shots:
                st = {}
                try:
                    new = S.ask(a.model, p, topic_list, pixels=w * h, stats=st)
                except Exception as e:
                    print(f"  ошибка {p.name}: {e}")
                    continue
                same += new["topic"] == old["topic"]
                secs.append(st["seconds"])
                toks.append(st["prompt"] or 0)
                f.write(json.dumps({"sha": sha, "src": str(p.relative_to(S.SRC)), "old": old["topic"],
                                    "old_title": old["title"], "new": new["topic"], "new_title": new["title"], **st},
                                   ensure_ascii=False) + "\n")
        n = len(secs) or 1
        line = (f"{size}: та же тема {same}/{len(secs)} ({same / n:.0%}), {sum(secs) / n:.1f} с/шт, "
                f"токенов запроса ~{sum(toks) // n}, всего {time.time() - t0:.0f} с")
        report.append(line)
        print(line, flush=True)
    print("\n".join(["", "Итог:"] + report))


if __name__ == "__main__":
    main()
