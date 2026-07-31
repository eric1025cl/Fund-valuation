# Fund Valuation

中国公募基金盘中估值 MVP。后端通过可选 AKShare 数据源读取官方估值、净值、持仓和 A 股行情；官方估值缺失时按披露持仓和实时行情做覆盖率归一化估算。

## 启动

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 估值口径

```text
估算涨跌 = Σ(持仓权重 × 标的实时涨跌) / 已覆盖持仓权重
估算净值 = 最新官方单位净值 × (1 + 估算涨跌)
```

估算结果仅供研究和看板参考，不构成投资建议。
