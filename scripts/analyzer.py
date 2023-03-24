from db_utils import *
import numpy as np
from tqdm import tqdm
import ast
import scipy.stats
from enum import Enum
from vbkv_filemap import *

from configs.projects import *
from configs.experiments import *
from plot_utils import *
import matplotlib
import matplotlib.pyplot as plt
from statsmodels.stats.proportion import proportions_ztest

COLORS = [
    "#FFB300", # Vivid Yellow
    "#803E75", # Strong Purple
    "#FF6800", # Vivid Orange
    "#A6BDD7", # Very Light Blue
    "#C10020", # Vivid Red
    "#CEA262", # Grayish Yellow
    "#817066", # Medium Gray
]

def get_color_map(keys):
    assert len(keys) <= len(COLORS)
    return {k: COLORS[i] for i, k in enumerate(keys)}

def percentage(a, b):
    return a * 100 / b

class RCode(Enum):
    SAT = 1
    UNSAT = 2
    TIMEOUT = 3
    UNKNOWN = 4
    ERROR = 5

    def from_str(s):
        if s == "sat":
            return RCode.SAT
        elif s == "unsat":
            return RCode.UNSAT
        elif s == "timeout":
            return RCode.TIMEOUT
        elif s == "unknown":
            return RCode.UNKNOWN
        elif s == "error":
            return RCode.ERROR
        else:
            assert False

    def __str__(self):
        if self == RCode.SAT:
            return "sat"
        elif self == RCode.UNSAT:
            return "unsat"
        elif self == RCode.TIMEOUT:
            return "timeout"
        elif self == RCode.UNKNOWN:
            return "unknown"
        elif self == RCode.ERROR:
            return "error"
        else:
            assert False

def build_solver_summary_table(cfg, solver):
    con, cur = get_cursor(cfg.qcfg.db_path)
    solver_table = cfg.qcfg.get_solver_table_name(solver)
    summary_table = cfg.get_solver_summary_table_name(solver)

    if not check_table_exists(cur, solver_table):
        con.close()
        return

    cur.execute(f"""DROP TABLE IF EXISTS {summary_table}""")

    cur.execute(f"""CREATE TABLE {summary_table} (
        vanilla_path TEXT,
        pretubrations TEXT,
        summaries BLOB)""")

    res = cur.execute(f"""
        SELECT DISTINCT(query_path), result_code, elapsed_milli
        FROM {solver_table}
        WHERE query_path = vanilla_path""")

    vanilla_rows = res.fetchall()
    for (vanilla_path, v_rcode, v_time) in tqdm(vanilla_rows):
        res = cur.execute(f"""
            SELECT result_code, elapsed_milli, perturbation FROM {solver_table}
            WHERE vanilla_path = "{vanilla_path}"
            AND perturbation IS NOT NULL""")

        perturbs = [str(p) for p in cfg.qcfg.enabled_muts]
        v_rcode = RCode.from_str(v_rcode).value
        results = {p: [[v_rcode], [v_time]] for p in perturbs}

        for row in res.fetchall():
            results[row[2]][0].append(RCode.from_str(row[0]).value)
            results[row[2]][1].append(row[1])

        mut_size = cfg.qcfg.max_mutants
        blob = np.zeros((len(perturbs), 2, mut_size + 1), dtype=int)
        for pi, perturb in enumerate(perturbs):
            (veri_res, veri_times) = results[perturb]
            blob[pi][0] = veri_res
            blob[pi][1] = veri_times

        cur.execute(f"""INSERT INTO {summary_table}
            VALUES(?, ?, ?);""", 
            (vanilla_path, str(perturbs), blob))

    con.commit()
    con.close()

def as_seconds(milliseconds):
    return milliseconds / 1000

def group_time_mean(times):
    assert len(times) != 0
    return as_seconds(np.mean(times))

def group_time_std(times):
    assert len(times) != 0
    return as_seconds(np.std(times))

def group_success_rate(vres):
    assert len(vres) != 0
    return percentage(vres.count("unsat"), len(vres))

# def split_summary_table(cfg):
#     con, cur = get_cursor(cfg.qcfg.db_path)
#     summary_table_name = cfg.get_summary_table_name()
#     # summaries = dict()

#     for solver in cfg.samples:
#         solver = str(solver)
#         new_table_name = cfg.qcfg.get_solver_table_name(solver) + "_summary"

#         res = cur.execute(f"""SELECT * FROM {summary_table_name}
#             WHERE solver = ?""", (solver,))
#         rows = res.fetchall()
#         if len(rows) == 0:
#             print(f"[INFO] skipping {summary_table_name} {solver}")
#             continue

#         cur.execute(f"""DROP TABLE IF EXISTS {new_table_name}""")

#         cur.execute(f"""CREATE TABLE {new_table_name} (
#             vanilla_path TEXT,
#             pretubrations TEXT,
#             summaries BLOB)""")

#         cur.execute(f"""
#             INSERT INTO {new_table_name} 
#             SELECT vanilla_path, pretubrations, summaries FROM {summary_table_name}
#             WHERE solver = ?""", (solver,))
#     con.commit()
#     con.close()

def load_solver_summary(cfg, solver):
    con, cur = get_cursor(cfg.qcfg.db_path)
    new_table_name = cfg.qcfg.get_solver_table_name(solver) + "_summary"
    if not check_table_exists(cur, new_table_name):
        print(f"[INFO] skipping {new_table_name}")
        return None
    solver = str(solver)

    res = cur.execute(f"""SELECT * FROM {new_table_name}""")
    rows = res.fetchall()

    nrows = []
    mut_size = cfg.qcfg.max_mutants
    for row in rows:
        perturbs = ast.literal_eval(row[1])
        blob = np.frombuffer(row[2], dtype=int)
        blob = blob.reshape((len(perturbs), 2, mut_size + 1))
        nrow = [row[0], perturbs, blob]
        nrows.append(nrow)
    con.close()
    return nrows

def load_solver_summaries(cfg):
    summaries = dict()

    for solver in cfg.samples:
        nrows = load_solver_summary(cfg, solver)
        if nrows is None:
            continue
        summaries[solver] = nrows
    return summaries

class Stablity(str, Enum):
    UNKOWN = "unknown"
    UNSOLVABLE = "unsolvable"
    RES_UNSTABLE = "res_unstable"
    TIME_UNSTABLE = "time_unstable"
    STABLE = "stable"

    def __str__(self) -> str:
        return super().__str__()

    def empty_map():
        em = {c: set() for c in Stablity}
        return em

# miliseconds
def count_within_timeout(blob, rcode, timeout=1e6):
    success = blob[0] == rcode.value
    none_timeout = blob[1] < timeout 
    success = np.sum(np.logical_and(success, none_timeout))
    return success

class Thresholds:
    def __init__(self, method):
        self.confidence = 0.05
        self.timeout = 1e6

        self.unsolvable = 5
        assert 0 < self.unsolvable < 100

        self.res_stable = 95
        assert 0 < self.res_stable < 100

        self.time_std = 1e6

        if method == "regression":
            self.categorize_group = self._categorize_group_regression
        elif method == "strict":
            self.categorize_group = self._categorize_group_divergence_strict
        elif method == "threshold":
            self.categorize_group = self._categorize_group_threshold
        else:
            assert False

    def _categorize_group_regression(self, group_blob):
        pres = group_blob[0][0]
        ptime = group_blob[1][0]
        if pres != RCode.UNSAT.value or ptime > self.timeout:
            return Stablity.UNSOLVABLE

        timeout = max(ptime * 1.5, ptime + 50000)
        success = count_within_timeout(group_blob, RCode.UNSAT, timeout)
        # if success < len(group_blob[0]) * 0.8:
        #     return Stablity.RES_UNSTABLE

        size = len(group_blob[0])
        if success != size:
            return Stablity.RES_UNSTABLE
        return Stablity.STABLE

    def _categorize_group_divergence_strict(self, group_blob):
        size = len(group_blob[0])
        success = count_within_timeout(group_blob, RCode.UNSAT, self.timeout)

        if success == 0:
            uks = count_within_timeout(group_blob, RCode.UNKNOWN, self.timeout)
            if uks == size:
                return Stablity.UNKOWN
            return Stablity.UNSOLVABLE

        if success == size:
            return Stablity.STABLE
        
        return Stablity.RES_UNSTABLE

    def _categorize_group_threshold(self, group_blob):
        # pres = group_blob[0][0]
        # ptime = group_blob[1][0]
        ress = group_blob[0]
        times = group_blob[1]

        size = len(ress)
        success = count_within_timeout(group_blob, RCode.UNSAT, self.timeout)

        value = self.unsolvable/100
        _, p_value = proportions_ztest(count=success,
                                        nobs=size,
                                        value=value, 
                                        alternative='smaller',
                                        prop_var=value)
        if p_value <= self.confidence:
            return Stablity.UNSOLVABLE

        value = self.res_stable / 100
        _, p_value = proportions_ztest(count=success, 
                                        nobs=size,
                                        value=value,
                                        alternative='larger',
                                        prop_var=value)

        if p_value <= self.confidence:
        #     std = np.std(times)
        #     time_std = self.time_std * 1000
        #     T = (size - 1) * ((std / time_std) ** 2)
        #     if T > scipy.stats.chi2.ppf(1-self.confidence, df=size-1):
        #         return Stablity.TIME_UNSTABLE
        #     else:
            return Stablity.STABLE

        return Stablity.RES_UNSTABLE
    
    def categorize_query(self, group_blobs, perturbs=None):
        ress = set()
        if perturbs is None:
            perturbs = [i for i in range(group_blobs.shape[0])]
        for i in perturbs:
            ress.add(self.categorize_group(group_blobs[i]))
        if len(ress) == 1:
            return ress.pop()
        if ress == {Stablity.TIME_UNSTABLE, Stablity.STABLE}:
            return Stablity.TIME_UNSTABLE
        return Stablity.RES_UNSTABLE

def categorize_qeuries(rows, thresholds, perturbs=None):
    categories = Stablity.empty_map()
    for query_row in rows:
        plain_path = query_row[0]
        res = thresholds.categorize_query(query_row[2], perturbs)
        categories[res].add(plain_path)
    return categories

def get_category_precentages(categories):
    percentages = dict()
    total = sum([len(i) for i in categories.values()])
    for c, i in categories.items():
        percentages[c] = percentage(len(i), total)
    return percentages, total

# def subplot_cutoff(sp, xs, ys0, ys1, ys2, solver):
#     sp.plot(xs, ys0, marker=",", label="unsolvables")
#     sp.plot(xs, ys1, marker=",", label="res_unstables")
#     sp.plot(xs, ys2, marker=",", label=" res_unstables")
#     sp.set_title(f'{solver} timelimit cutoff vs category precentage')
#     sp.set_xlabel("timelimit selection (seconds)")
#     sp.set_ylabel("precentage of query")

#     sp.legend()

def plot_cutoff(cfg):
    s = load_solver_summaries(cfg)
    solver_count = len(s.keys())
    cut_figure, cut_aixs = setup_fig(solver_count, 2)
    xs = [i for i in range(5, 61, 1)]
    perturbs = [str(p) for p in cfg.qcfg.enabled_muts]

    for j, (solver, rows) in enumerate(s.items()):
        sps = cut_aixs
        if solver_count != 1:
            sps = cut_aixs[j]

        strict_th = Thresholds("strict")
        palin_th = Thresholds("regression")

        stricts = {"unsolvable": [], "union": [], "shuffle": [], 
                    "rename": [], "rseed": [], "intersect": []}
        plains = {"unsolvable": [], "res_unstable": []}

        for i in xs:
            strict_th.timeout = i * 1000
            palin_th.timeout = i * 1000

            categories = {"unsolvable": set(), "shuffle": set(), "rename":set(), "rseed": set(), "union": set()}
            categories2 = {"unsolvable": 0, "res_unstable": 0, "stable": 0}
            for query_row in rows:
                plain_path = query_row[0]
                group_blobs = query_row[2]
                ress = set()
                for k, p in enumerate(perturbs):
                    res = strict_th.categorize_group(group_blobs[k])
                    if res == Stablity.RES_UNSTABLE:
                        categories[p].add(plain_path)
                    ress.add(res)
                if ress == {Stablity.UNSOLVABLE}:
                    categories["unsolvable"].add(plain_path)
                elif ress != {Stablity.STABLE}:
                    categories["union"].add(plain_path)

                res = palin_th.categorize_query(group_blobs)
                categories2[res] += 1

            total = len(rows)
            intersect = set.intersection(*[categories["shuffle"], categories["rename"], categories["rseed"]])
            categories["intersect"] = intersect
            for k, v in categories.items():
                stricts[k].append(percentage(len(v) , total))
            for k in {"unsolvable", "res_unstable"}:
                plains[k].append(percentage(categories2[k], total))
        for k in stricts:
            sps[0].plot(xs, stricts[k], marker="o", label=k)
        sps[0].legend()
        sps[0].set_xlim(left=5, right=60)
        sps[0].set_ylim(bottom=0, top=8)
        sps[0].set_title(f"{solver} timelimit cutoff vs category precentage [divergence]")
        sps[0].set_xlabel("timelimit selection (seconds)")

        for k in plains:
            sps[1].plot(xs, plains[k], marker="o", label=k)
        sps[1].legend()
        sps[1].set_xlim(left=5, right=60)
        sps[1].set_ylim(bottom=0, top=8)
        sps[1].set_title(f"{solver} timelimit cutoff vs category precentage [regression]")
        sps[1].set_xlabel("timelimit selection (seconds)")

    name = cfg.qcfg.name
    save_fig(cut_figure, f"{name}", f"fig/time_cutoff/{name}.png")

def categorty_prediction(cfg):
    summaries = load_solver_summaries(cfg)
    sample_size = 30
    for solver in summaries:
        true_unsol, est_unsol = 0, 0
        true_stable, est_stable = 0, 0
        for query_row in summaries[solver]:
            group_blobs = query_row[2]
            for i in range(group_blobs.shape[0]):
                sample = group_blobs[i][:,:sample_size]
                sample_success = successes_within_timeout(sample)
                true_success = successes_within_timeout(group_blobs[i])
                if sample_success == 0:
                    est_unsol += 1
                    if true_success == 0:
                        true_unsol += 1
                if sample_success == sample_size:
                    est_stable += 1
                    if true_success == group_blobs.shape[2]:
                        true_stable += 1
        print(solver, 
              round(percentage(true_unsol, est_unsol), 2),
              round(percentage(true_stable, est_stable), 2))

def compare_perturbations(cfg, solver=None):
    summaries = load_solver_summaries(cfg)
    th = Thresholds("strict")

    # votes = {c: 0 for c in Stablity}
    if solver is not None:
        summaries = {solver: summaries[solver]}

    solver_count = len(summaries.keys())
    figure, aixs = setup_fig(solver_count, 2)

    for j, solver in enumerate(summaries):
        decisions = dict()
        perturbations = summaries[solver][0][2]
        categories = [c for c in Stablity]
        for p in perturbations:
           decisions[p] = {c: set() for c in Stablity}
        for query_row in summaries[solver]:
            group_blobs = query_row[2]
            for i in range(group_blobs.shape[0]):
                p = perturbations[i]
                c = th.categorize_group(group_blobs[i])
                decisions[p][c].add(query_row[0])
        sps = aixs
        if solver_count != 1:
            sps = aixs[j]
        data = []
        for (_, decision) in decisions.items():
            pts = []
            for c in categories:
                pts.append(len(decision[c]))
            data.append(pts)
        data = np.array(data)

        new_row = np.zeros(data.shape[1])
        for i, c in enumerate(categories):
            things = [decisions[p][c] for p in perturbations]
            common = set.intersection(*things)
            new_row[i] = len(common)
        data = np.vstack((data, new_row))
        perturbations += ["common"]

        norm_data = np.zeros(data.T.shape)
        for i, col in enumerate(data.T):
            new_col = np.zeros(col.shape)
            if np.max(col) != 0:
                new_col = np.array(col) / np.max(col)
            norm_data[i] = new_col
        norm_data = norm_data.T

        bar_width = len(categories)/50
        br = np.arange(len(categories))
        br = [x - bar_width for x in br]

        for i in range(data.shape[0]):
            if i == len(data) - 1:
                continue
            br = [x + bar_width for x in br]
            sps[0].bar(br, height=data[i]-data[-1], bottom=data[-1], width=bar_width, color=COLORS[i], label=str(perturbations[i]))
            sps[0].bar(br, height=data[-1], width=bar_width, color=COLORS[i], alpha=0.5)
            sps[1].bar(br, height=norm_data[i]-norm_data[-1], bottom=norm_data[-1], width=bar_width, color=COLORS[i], label=str(perturbations[i]))
            sps[1].bar(br, height=norm_data[-1], width=bar_width, color=COLORS[i], alpha=0.5)

        sps[0].set_xticks([r + bar_width for r in range(len(categories))])
        sps[0].set_xticklabels([str(c) for c in categories])
        sps[0].set_xlabel('categorties', fontsize = 12)
        sps[0].legend()

        sps[1].set_xticks([r + bar_width for r in range(len(categories))])
        sps[1].set_xticklabels([str(c) for c in categories])
        sps[1].set_xlabel('categorties', fontsize = 12)
        sps[1].legend()

    name = cfg.qcfg.name
    plt.savefig(f"fig/pert_diff/{name}.png")

def export_timeouts(cfg, solver):
    # th.timeout = threshold * 1000

    con, cur = get_cursor(cfg.qcfg.db_path)
    solver_table = cfg.qcfg.get_solver_table_name(solver)

    if not check_table_exists(cur, solver_table):
        print(f"[WARN] export timeout: {solver_table} does not exist!")
        con.close()
        return
    clean_dir = cfg.qcfg.project.clean_dirs[solver]
    assert clean_dir.endswith("/")
    target_dir = clean_dir[:-1] + "_"+ str(solver) + "_ext/"

    res = cur.execute(f"""
        SELECT vanilla_path, perturbation, command FROM {solver_table}
        WHERE result_code = "timeout" """)

    rows = res.fetchall()
    # print(len(rows))

    for row in rows:
        vanilla_path = row[0]
        perturb = row[1]
        assert vanilla_path.endswith(".smt2")
        assert vanilla_path.startswith(clean_dir)
        stemed = vanilla_path[len(clean_dir):-5]
        command = row[2]
        [solver_path, mut_path, limit] = command.split(" ")
        index = mut_path.index(stemed) + len(stemed)
        info = mut_path[index:].split(".")
        # print(vanilla_path)
        if perturb is None:
            command = f"cp {vanilla_path} {target_dir}"
        else:
            seed = int(info[1])
            assert perturb == info[2]
            file_name = f"{str(seed)}.{perturb}.smt2"
            mutant_path = target_dir + stemed + "." + file_name
            command = f"./target/release/mariposa -i {vanilla_path} -p {perturb} -o {mutant_path} -s {seed}"
        print(command)

    con.close()

def plot_query_sizes(cfgs):
    import os
    # figure, axis = setup_fig(1, 2)
    colors = get_color_map([cfg.qcfg.name for cfg in cfgs])

    for cfg in cfgs:
        clean_dir = cfg.qcfg.project.clean_dirs[Z3_4_11_2]
        paths = list_smt2_files(clean_dir)
        sizes = [] 
        for path in paths:
            sizes.append(os.path.getsize(path) / 1024)
        n = len(sizes)
        label = cfg.qcfg.name
        color = colors[label]
        plt.plot(np.sort(sizes), np.arange(n), marker=",", label=label, color=color)

    plt.legend()
    plt.xscale("log")
    plt.ylabel("cumulative count")
    plt.xlabel("query size KB (log scale)")

    plt.tight_layout()
    plt.savefig("fig/sizes.pdf")

def plot_stacked_bars(data):
    assert len(data.shape) == 3

    bar_width = len(data.shape[1]) / 100
    fig, ax = plt.subplots()

    for pi, project_row in enumerate(data):
        pcs = np.zeros((data.shape[2], data.shape[1]))
        br = [x + bar_width for x in br]
        for i, ps in enumerate(project_row):
            pcs[:, i] = ps
        pcolor = COLORS[pi]
        pcs = np.cumsum(pcs,axis=0)

        plt.bar(br, height=pcs[0], width=bar_width, color=pcolor, alpha=0.10, edgecolor='black', hatch='/////')
        plt.bar(br, height=pcs[1]-pcs[0], bottom=pcs[0], width=bar_width, color=pcolor, alpha=0.40, edgecolor='black')
        plt.bar(br, height=pcs[2]-pcs[1], bottom=pcs[1], width=bar_width, color=pcolor, label=project_names[pi], edgecolor='black')


def dump_all(cfgs):
    projects = [cfg.qcfg.project for cfg in cfgs]
    project_names = [cfg.get_project_name() for cfg in cfgs]
    solver_names = [str(s) for s in Z3_SOLVERS_ALL]

    category_count = len(Stablity)
    thres = Thresholds("strict")
    thres.timeout = 3e4 
    data = np.zeros((len(cfgs), len(solver_names), category_count))
    for cfg in cfgs:
        summaries = load_solver_summaries(cfg)
        for solver in tqdm(solver_names):
            if solver in summaries:
                items = categorize_qeuries(summaries[solver], thres)
                ps, _ = get_category_precentages(items)
                ps = [ps[c] for c in Stablity]
                data[cfgs.index(cfg), solver_names.index(solver)] = ps

    data = np.array(data)

    bar_width = len(solver_names)/100
    fig, ax = plt.subplots()

    br = np.arange(len(solver_names))
    br = [x - bar_width for x in br]

    # data[solver_index][project_index][category_index]

    for pi, project_row in enumerate(data):
        pcs = np.zeros((category_count, len(solver_names)))
        br = [x + bar_width for x in br]
        for i, ps in enumerate(project_row):
            pcs[:, i] = ps
        pcolor = COLORS[pi]
        pcs = np.cumsum(pcs,axis=0)

        plt.bar(br, height=pcs[0], width=bar_width, color=pcolor, alpha=0.10, edgecolor='black', hatch='/////')
        plt.bar(br, height=pcs[1]-pcs[0], bottom=pcs[0], width=bar_width, color=pcolor, alpha=0.40, edgecolor='black')
        plt.bar(br, height=pcs[2]-pcs[1], bottom=pcs[1], width=bar_width, color=pcolor, label=project_names[pi], edgecolor='black')

        # for i, ps in enumerate(project_row):
        #     if projects[pi].orig_solver == solver_names[i]:
        #         plt.bar(br, height=pcs[2]-pcs[1], bottom=pcs[1], width=bar_width, color=pcolor, hatch='////')

    plt.ylim(bottom=0, top=15)
    plt.xlabel('solvers', fontsize = 12)
    plt.ylabel('categorty ratios', fontsize = 12)
    solver_lables = [f"{str(s).replace('_', '.')}\n{s.data[:-3]}" for s in Z3_SOLVERS_ALL]
    ax.tick_params(axis='both', which='major', labelsize=8)
    plt.xticks([r + bar_width for r in range(len(solver_names))], solver_lables, rotation=30, ha='right')
    plt.legend()
    plt.tight_layout()
    plt.savefig("fig/all.pdf")

# def map_query_to_file(files, query):
#     for f in files:
#         if query in f:
#             return f
#     return None

def compare_vbkvs(linear, dynamic):
    dfiles, lfiles = set(), set()
    for k, v in FILE_MAP.items():
        dfiles |= set(v[0])
        lfiles |= set(v[1])
    # print(len(lfiles))
    # print(len(dfiles))

    th = Thresholds("strict")
    # th.timeout = 3e4 # 30s
    # th.unsolvable = 20
    # th.res_stable = 80

    linear_filtered = set()
    for query in linear.samples[Z3_4_11_2]:
        for f in lfiles:
            if "-" + f in query:
                linear_filtered.add(query)
    dynamic_filtered = set()
    for query in dynamic.samples[Z3_4_11_2]:
        for f in dfiles:
            if "-" + f in query:
                dynamic_filtered.add(query)
                break

    print(len(linear_filtered))
    print(len(dynamic_filtered))

    for solver in Z3_SOLVERS_ALL:
        linear_summary = load_solver_summary(linear, solver)
        if linear_summary is None:
            continue

        linear_categories = categorize_qeuries(linear_summary, th)

        linear_filtered_categories = {c: set() for c in Stablity}
        for c, qs in linear_categories.items():
            linear_filtered_categories[c] = qs.intersection(linear_filtered)
        # print(get_category_precentages(linear_filtered_categories))

        dynamic_summary = load_solver_summary(dynamic, solver)
        if dynamic_summary is None:
            continue
        d_categories = categorize_qeuries(dynamic_summary, th)
        # dynamic_all = set.union(*[v for v in dynamic.values()])

        dynamic_filtered_categories = {c: set() for c in Stablity}

        for c, qs in d_categories.items():
            dynamic_filtered_categories[c] = qs.intersection(dynamic_filtered)

        for c, qs in linear_filtered_categories.items():
            print(c, len(qs))
        print(" ")
        for c, qs in dynamic_filtered_categories.items():
            print(c, len(qs))
        print("----")

# def dump_unsolvable(cfgs, timeout_threshold):
#     for cfg in cfgs:
#         summaries = load_summary(cfg, timeout_threshold)
#         categories = get_categories(summaries)
#         for solver, (unsolvables, _, _, _) in categories.items():
#             lname = f"data/sample_lists/{cfg.qcfg.name}_UNSOL_{solver}"
#             f = open(lname, "w+")
#             for item in unsolvables:
#                 f.write(item + "\n")
#             f.close()
