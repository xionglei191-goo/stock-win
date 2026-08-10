# 通达信缠论交易系统 V1

该目录是独立于通达信安装目录的研究、扫描、回测和模拟交易层。默认不会调用真实交易接口。

## 运行条件

- Windows
- 已启动并登录 `D:\Project\stock\tdx-mock\TdxW.exe`
- Python 3，并安装 `pandas`、`numpy`
- 通达信已下载日线行情

## 使用

首次运行会读取行业和概念板块，并在 `outputs/cache` 缓存成分股：

```powershell
cd D:\Project\stock
py -3 strategy_v1\run_scan.py --dry-run --refresh-sectors
```

以后执行扫描：

```powershell
py -3 strategy_v1\run_scan.py --dry-run
```

确认结果后，显式回传通达信：

```powershell
py -3 strategy_v1\run_scan.py --push-tdx
```

历史滚动回测：

```powershell
py -3 strategy_v1\backtest.py
```

运行单元测试：

```powershell
py -3 -m unittest discover -s strategy_v1\tests -v
```

连接已启动的通达信执行数据集成测试：

```powershell
$env:TDX_INTEGRATION='1'
py -3 -m unittest strategy_v1.tests.test_tdx_integration -v
```

`--max-stocks N` 仅用于快速诊断，正式扫描和回测不要设置该参数。

## 输出

- `outputs/signals.csv`：扫描信号
- `outputs/trades.csv`：模拟成交
- `outputs/positions.json`：模拟账户状态和待成交订单
- `outputs/equity.csv`：模拟账户净值
- `outputs/backtest_*.csv`：滚动回测结果
- `outputs/backtest_summary.json`：回测摘要

结构分析使用前复权行情；成交模拟使用不复权行情。买卖信号在日线收盘后确认，订单在下一交易日开盘模拟成交，并执行 T+1、涨跌停、手续费和滑点规则。
