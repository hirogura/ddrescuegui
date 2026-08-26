# ddrescueGUI

ブラウザから ddrescue を操作できる Web インターフェースです。
レスキュー（コピー）の実行、進行状況のリアルタイム表示、ログの管理ができます。

- 動作環境: Debian / Ubuntu（systemd を使用）
- デフォルトポート: **3327**
- 使用ツール: `ddrescue`（gddrescue）, `smartmontools`, `fdisk`, `lsblk`

## インストール

root 権限で実行します。スクリプトは GitHub から最新版をダウンロード（または更新）し、systemd サービスとして登録します。

```bash
sudo wget -O /tmp/ddrescuegui-install.sh \
  https://raw.githubusercontent.com/hirogura/ddrescuegui/main/install.sh
sudo bash /tmp/ddrescuegui-install.sh
```

インストール完了後、ブラウザで以下にアクセスします。

```
http://<サーバーのIP>:3327
```

`install.sh` は /opt/ddrescuegui へ自動的にインストールします。

Tailscale が導入済みの環境では、インストーラが自動で Tailscale Serve を設定し、
Tailnet 内のみ HTTPS（`https://<マシン名>.<tailnet>.ts.net:3327`）で公開します。
手動で設定する場合は以下を実行してください。

```bash
sudo tailscale serve --bg --https=3327 http://127.0.0.1:3327
```

### 更新（アップグレード）

インストールスクリプトを再度実行するだけで、GitHub の最新版に更新されます。

```bash
sudo bash /tmp/ddrescuegui-install.sh
```

## アンインストール

サービスを停止・無効化し、設定ファイルとインストール先を削除します。

```bash
sudo systemctl stop ddrescuegui
sudo systemctl disable ddrescuegui
sudo rm /etc/systemd/system/ddrescuegui.service
sudo systemctl daemon-reload
sudo rm -rf /opt/ddrescuegui
```

ログ（ddrescue の実行履歴・マップファイル）も削除されます。保存したい場合は削除前にバックアップしてください。

### サービス管理

```bash
sudo systemctl status ddrescuegui   # 状態確認
sudo systemctl restart ddrescuegui  # 再起動
sudo journalctl -u ddrescuegui -f   # ログ表示
```

## ディレクトリ構成

```
/opt/ddrescuegui/
├── server.py          # Web サーバー本体
├── public/index.html  # Web UI
├── logs/              # 実行ログ・マップファイル（自動生成）
└── install.sh         # インストーラ
```

## 注意事項

- 復旧対象のディスクを誤指定しないよう、実行前にデバイスのサイズ・モデル・シリアル番号を必ず確認してください。
- 実行ログ（`logs/`）にはデバイス情報が含まれるため、リポジトリには公開されません（`.gitignore` で除外）。

## ライセンス

MIT License です。詳細は [LICENSE](./LICENSE) を参照してください。
