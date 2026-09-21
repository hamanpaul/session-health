# 修正批次聚合雷達

批次 HTML 現在以一張聚合雷達呈現七軸平均，legacy 只納入未 failed 的 session，並在
每軸標示 `n/N`；逐 session 的 legacy mini-radar grid 移除，session comparison table／bar
保留。process-v2 只平均明確有效的 observed ratio，排除 `unknown` 與 `not_applicable`
而不補零；七軸各有觀測時即使分母不同仍繪製 radar，任一軸 `n=0` 則省略 polygon 並顯示
coverage 缺口。單一 session radar 行為與 process-v2 heatmap／coverage bars 維持。
