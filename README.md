# 新規会員入室Bot 最終版

## 構成

1. 新規入室BotでPayPay 3,000円を支払う
2. 会員が「入金完了」を押す
3. 管理画面でPayPay履歴を照合して承認
4. 承認済み会員だけ本人専用リンクを発行
5. Telegram IDを照合してグループ参加を承認

## Railway Variables

- BOT_TOKEN
- DATABASE_URL
- GROUP_ID
- ADMIN_ID=8245808922
- ADMIN_PASSWORD
- SECRET_KEY
- MEMBERSHIP_FEE=3000
- QR_EXPIRES_AT=2026-08-05 07:09 JST

## グループ設定

このBotを入室先グループの管理者にしてください。

必要な権限：

- 招待リンク作成
- 参加申請承認

## 管理画面

Railwayで公開ドメインを作り、末尾に `/admin` を付けます。

例：

https://xxxx.up.railway.app/admin

## QR差し替え

期限前に `assets/paypay_qr.jpg` を新しいQR画像で上書きしてください。
