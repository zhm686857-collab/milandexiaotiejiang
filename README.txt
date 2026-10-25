清简账本（Windows 本地财务管理）
================================

运行：双击 LocalFinance.exe。

数据库 personal_finance.db 会自动创建在 LocalFinance.exe 同级目录。
请把整个 LocalFinance 文件夹放在桌面、文档或其他可写目录；不要放入
C:\Program Files，否则 Windows 可能禁止创建数据库。

开发构建：
1. 安装 Python 3.13。
2. 双击 build_windows.bat。
3. 成品位于 dist\LocalFinance\LocalFinance.exe。

重要特性：
- 金额以整数“分”保存。
- 双分录、事务过账、资产账户禁止负数。
- 主钱包总额由全部资产钱包实时求和。
- 历史删除采用冲销而非物理删除。
- 周期预算支持结转、超支重算和低预算提醒。
