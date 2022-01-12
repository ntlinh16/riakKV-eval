import math
import matplotlib.pyplot as plt
from pathlib import Path
import sys

import pandas as pd

plt.rcParams.update({'font.size': 30.0})

result_path = Path(sys.argv[1])

if len(sys.argv) >= 3:
    plot_by = sys.argv[2]
else:
    plot_by = 'n_nodes'

df = pd.read_csv(result_path)
groupby_cols = ['concurrent_clients', 'n_nodes']
if plot_by != 'n_nodes':
    groupby_cols = ['n_dc'] + groupby_cols
df = df.groupby(groupby_cols).mean().reset_index()
print(f'Plot data: {df}')


def plot(df, label, color, marker, linewidth=5, markersize=20, annotations=None, x_range=None, y_range=None):
    x = df['throughput']
    y = df['latency']

    if x_range is None:
        min_x = round(math.floor(x.min() - 1000), -3)
        max_x = round(math.ceil(x.max() + 1000), -3)
    else:
        min_x, max_x = x_range

    if y_range is None:
        min_y = 0
        max_y = round(math.ceil(y.max() + 10), -3)
    else:
        min_y, max_y = y_range

    plt.plot(x, y,
             linewidth=linewidth,
             markersize=markersize,
             color=color,
             marker=marker,
             markeredgewidth=4,
             fillstyle='none',
             label=label)
    ax = plt.gca()
    ax.set_xticks(range(min_x, max_x + 1, 1000))
    ax.set_yticks(range(min_y, max_y + 1, 20))
    if annotations:
        # ax = plt.gca()
        for i, text in enumerate(annotations):
            if i + 1 > len(x):
                break
            t = plt.text(x.iloc[i] + 90, y.iloc[i] + 1.5, text)
            # t.set_bbox(dict(facecolor='white', edgecolor='white'))
            # ax.annotate(text, (x.iloc[i] - 100, y.iloc[i] + 2))


x_range = [2_000, 6_000]
y_range = [0, 160]
if plot_by == 'n_nodes':
    plot(df[df['n_nodes'] == 6], "6 nodes (ring_size = 64)", "goldenrod", "X",
         x_range=x_range, y_range=y_range)
    plot(df[df['n_nodes'] == 9], "9 nodes (ring_size = 128)", "mediumblue", "d",
         x_range=x_range, y_range=y_range)
    plot(df[df['n_nodes'] == 12], "12 nodes (ring_size = 256)", "red", "o",
         annotations=[32, 64, 128, 256, 512], x_range=x_range, y_range=y_range)
else:
    plot(df[df['n_dc'] == 1], "1 DC (6 nodes, ring_size = 64)",
         "goldenrod", "X", x_range=x_range, y_range=y_range)
    plot(df[df['n_dc'] == 2], "2 DCs", "mediumblue", "d", x_range=x_range, y_range=y_range)
    plot(df[df['n_dc'] == 3], "3 DCs", "red", "o", x_range=x_range, y_range=y_range,
         annotations=[36, 72, 144, 288, 576])

plt.legend(loc='upper left')
plt.grid()
ax = plt.gca()
ax.set_xlabel("Throughput (ops/s)")
ax.set_ylabel("Latency (ms)")

fig = plt.gcf()
fig.set_size_inches(20.5, 12.5)
plt.tight_layout()
fig.savefig(result_path.parent / 'plot.png', format="png", dpi=300)
