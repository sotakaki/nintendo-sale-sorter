# nintendo-sale-sorter

ニンテンドーストア(store-jp.nintendo.com)のセール中ソフトを全件取得し、
公式にはない「値引き率順」で並べたページを毎朝07:00(JST)に自動生成します。

**ページ**: https://sotakaki.github.io/nintendo-sale-sorter/

## 機能

- 値引き率順/価格順ソート、タイトル検索、値引き率・価格上限フィルタ
- Steamレビューのマージ(タイトル名の自動マッチング。Switch版の評価ではなく参考情報)
- ゲームカタログ@Wikiの判定表示(`gc_catalog.json`。atwikiはボット保護下のため自動取得はせず、一覧ページをブラウザで閲覧・抽出した静的データを手動更新)
- 過去最安トラッキング(2026-08-14からの自前蓄積。それ以前の履歴は含まない)

## 仕組み

1. SLAS (Salesforce Commerce Cloud) のゲスト認証(PKCE)で匿名トークンを取得
2. ストア検索APIを人気順+新着順の2周で全ページ取得(順位変動による取りこぼし対策)
3. Steamストア検索でマッチング→レビュー好評率を取得(`steam_cache.json` に差分キャッシュ)
4. セール価格を `price_history.json` に記録して過去最安を判定
5. `docs/index.html` に出力し、GitHub Pagesで配信

実行は GitHub Actions (`.github/workflows/build.yml`)。依存はPython標準ライブラリのみ。

ローカル実行:

```bash
python nintendo_sale.py                  # 通常実行(Steam照会は差分のみ)
python nintendo_sale.py --steam-backfill # Steam照会キャップなし(初回向け)
```

出力先は環境変数 `NINTENDO_SALE_OUT` で変更可能(未設定時は `~/Documents/nintendo_sale_sorted.html`)。
