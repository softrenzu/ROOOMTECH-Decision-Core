from __future__ import annotations

import random


DATASET_NAME = "ROOOMTECH Japanese Hospitality Intent Benchmark"
DATASET_VERSION = "1.0"
DATASET_ORIGIN = (
    "Independently authored synthetic benchmark data by ROOOMTECH; "
    "no third-party service outputs or proprietary datasets."
)

BASES = {
    "checkin_question": [
        "チェックインは何時からできますか？",
        "到着が{time}頃になりそうですが大丈夫ですか？",
        "早めにチェックインできますか？",
        "チェックイン方法を教えてください。",
        "鍵の受け取り方法はどうなっていますか？",
        "入室できる時間を確認したいです。",
        "{time}に着く予定です。チェックインできますか？",
        "セルフチェックインの手順を教えてください。",
        "チェックイン前に荷物を置けますか？",
        "到着したら最初に何をすればいいですか？",
    ],
    "access_problem": [
        "玄関の鍵が開きません。",
        "暗証番号を入力してもドアが開きません。",
        "部屋に入れなくて困っています。",
        "キーボックスが見つかりません。",
        "案内された鍵の番号で開きません。",
        "入口がどこか分かりません。",
        "建物のオートロックを通れません。",
        "鍵を取り出せません。",
        "ドアがロックされたままです。",
        "今、施設の前ですが入室できません。",
    ],
    "equipment_problem": [
        "お湯が出ません。",
        "エアコンが動きません。",
        "Wi-Fiにつながりません。",
        "テレビの電源が入りません。",
        "洗濯機が動かないです。",
        "トイレが流れません。",
        "シャワーの水圧がほとんどありません。",
        "冷蔵庫が冷えていません。",
        "電子レンジが使えません。",
        "ドライヤーが故障しているようです。",
    ],
    "complaint": [
        "部屋が汚れていて不満です。",
        "写真と実際の部屋がかなり違います。",
        "清掃ができていません。",
        "騒音がひどくて眠れませんでした。",
        "においが強くて快適に過ごせません。",
        "返金を希望します。",
        "この状態では宿泊できません。",
        "対応が遅すぎます。",
        "シーツに汚れがありました。",
        "説明と違うので納得できません。",
    ],
    "reservation_change": [
        "宿泊日を変更したいです。",
        "予約人数を{people}名に変更できますか？",
        "チェックアウト日を延ばしたいです。",
        "予約をキャンセルしたいです。",
        "一泊追加できますか？",
        "到着日を一日後ろに変更したいです。",
        "人数を減らしたいです。",
        "予約内容を変更する方法を教えてください。",
        "日程変更は可能でしょうか？",
        "もう一泊延長したいです。",
    ],
    "payment_question": [
        "支払い方法を教えてください。",
        "領収書を発行できますか？",
        "追加料金はいくらですか？",
        "クレジットカードで支払えますか？",
        "宿泊料金の内訳を確認したいです。",
        "請求金額が予約時と違うようです。",
        "デポジットは必要ですか？",
        "領収書の宛名を変更できますか？",
        "支払いはいつ確定しますか？",
        "清掃料金は宿泊料金に含まれていますか？",
    ],
}

PREFIXES = [
    "",
    "すみません、",
    "お世話になります。",
    "確認ですが、",
    "質問です。",
    "今困っているのですが、",
]
SUFFIXES = [
    "",
    " よろしくお願いします。",
    " 急ぎで確認したいです。",
    " ご確認お願いします。",
    " 教えてください。",
    " 可能でしょうか？",
]
TIMES = ["14時", "15時", "16時", "18時", "20時", "22時"]
PEOPLE = ["2", "3", "4", "5", "6"]


def build_dataset(seed: int = 42) -> dict:
    rng = random.Random(seed)
    examples: list[dict[str, str]] = []
    for label, bases in BASES.items():
        candidates: list[str] = []
        for base in bases:
            for prefix in PREFIXES:
                for suffix in SUFFIXES:
                    rendered = base.format(
                        time=rng.choice(TIMES),
                        people=rng.choice(PEOPLE),
                    )
                    candidates.append((prefix + rendered + suffix).strip())
        unique = list(dict.fromkeys(candidates))
        rng.shuffle(unique)
        chosen = unique[:60]
        if len(chosen) != 60:
            raise RuntimeError(f"not enough unique examples for {label}")
        for index, text in enumerate(chosen):
            examples.append(
                {
                    "text": text,
                    "label": label,
                    "split": "train" if index < 42 else "test",
                }
            )
    return {
        "name": DATASET_NAME,
        "version": DATASET_VERSION,
        "language": "ja",
        "origin": DATASET_ORIGIN,
        "labels": list(BASES.keys()),
        "examples": examples,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_dataset(), ensure_ascii=False, indent=2))
