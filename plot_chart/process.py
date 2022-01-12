from pathlib import Path
import math
import sys
import warnings

import pandas as pd

warnings.filterwarnings('ignore')

N_WINDOWS = 20

# path to directory contains experiment results
result_path = sys.argv[1]


def _path_2_comb(comb_dir_name):
    comb = comb_dir_name.replace('/', ' ').strip()
    i = iter(comb.split('-'))
    comb = dict(zip(i, i))
    comb['dirname'] = comb_dir_name
    return comb


def calc_throughput_latency(path):
    dfs = list()
    result_dirpath = None
    # for filepath in glob('*_latencies.csv'):
    for filepath in Path(path).rglob('*_latencies.csv'):
        operation_name = str(filepath).split('_latencies.csv')[0]
        result_dirpath = filepath.parent
        df = pd.read_csv(filepath)
        df.columns = [col.strip() for col in df.columns.tolist()]
        df = df[df['window'] >= 9].copy()
        df['elapsed'] = df['elapsed'].apply(lambda x: int(math.floor(x)))
        df['n'] = df['n'] / df['window']
        for col in ['min', 'mean', 'median', '95th', '99th', '99_9th', 'max']:
            # convert microseconds to milliseconds
            df[col] = df[col].apply(lambda x: x / 1000)
        dfs.append(df)
    df = pd.concat(dfs)

    data = list()
    for index, group in df.groupby('elapsed'):
        t = {
            'elapsed': index,
            'n': group['n'].sum(),
            'errors': group['errors'].sum()
        }
        for col in ['min', 'mean', 'median', '95th', '99th', '99_9th', 'max']:
            t[col] = group[col].mean()
        data.append(t)

    df2 = pd.DataFrame(data)

    MA_col = f'n_MA{N_WINDOWS}'
    df2 = df2[df2['n'] > 0]
    df2[MA_col] = df2['n'].rolling(window=N_WINDOWS).mean()

    # interpolate missing values
    alpha = df2['n'].max()
    beta = -math.log(df2[MA_col].iloc[N_WINDOWS - 1] / alpha) / (N_WINDOWS - 1)
    for i in range(0, N_WINDOWS - 1):
        df2[MA_col].iloc[i] = alpha * math.exp(-beta * i)

    df2[f'{MA_col}_diff'] = df2[MA_col].diff(periods=1).rolling(window=N_WINDOWS).mean().abs()

    THRESHOLD = 0.7
    DIFF_THRESHOLD = df2[f'{MA_col}_diff'].tail(int(THRESHOLD * len(df2))).mean()
    for i in range(0, len(df2)):
        if df2[f'{MA_col}_diff'].iloc[i] <= DIFF_THRESHOLD:
            break

    print(f'measuring point: {i}')

    throughput = df2[MA_col][i:].mean()
    latency = df2['mean'][i:].mean()

    df2.to_csv(result_dirpath / 'combine.csv', index=False)
    return df2, throughput, latency


p = Path(result_path)
df = pd.DataFrame([_path_2_comb(dirpath.name) for dirpath in p.iterdir()])
df.dropna(subset=['iteration'], inplace=True)
for col in df.columns:
    try:
        df = df.astype({col: int})
    except ValueError:
        print(col)
        continue

n_nodes_col = [col for col in df.columns if 'per_dc' in col and 'fmke' not in col][0]
print(n_nodes_col)
df['total_conn'] = df['n_fmke_client_per_dc'] * df['concurrent_clients']
df.sort_values([n_nodes_col, 'total_conn'], inplace=True)

has_dc_col = False
if 'n_dc' in df.columns:
    has_dc_col = True
    df = df.astype({'n_dc': int})
    df['total_conn'] = df['total_conn'] * df['n_dc']
    df.sort_values(['n_dc', n_nodes_col, 'total_conn'], inplace=True)

data = list()
for index, row in df.iterrows():
    print(f'Working on {row["dirname"]}')
    try:
        dirpath = p / row['dirname']
        df2, throughput, latency = calc_throughput_latency(str(dirpath))
        df2.to_csv(dirpath / 'final_combine.csv', index=False)
        cur_data = {
            'n_nodes': int(row[n_nodes_col]),
            'concurrent_clients': row['total_conn'],
            'iteration': row['iteration'],
            'throughput': throughput,
            'latency': latency,
        }
        if has_dc_col:
            cur_data['n_dc'] = int(row['n_dc'])
        data.append(cur_data)
    except Exception as e:
        print(f'--> Exception {e} on {row["dirname"]}')

df_final = pd.DataFrame(data)
df_final.sort_values(['n_nodes', 'concurrent_clients', 'iteration'], inplace=True)
if has_dc_col:
    df_final.sort_values(['n_dc', 'n_nodes', 'concurrent_clients', 'iteration'], inplace=True)
df_final.to_csv(p / 'result.csv', index=False)
print(f'\nResults: {df_final}')
