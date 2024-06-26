"""
|======================================== phd_filter_sim.py =======================================|
|                                                                                                  |
|   Property of Daniel Sturdivant. Unauthorized copying of this file via any medium would be       |
|   super sad and unfortunate for me. Proprietary and confidential.                                |
|                                                                                                  |
| ------------------------------------------------------------------------------------------------ |
|                                                                                                  |
|   @file     scripts/phd_filter_sim.py                                                            |
|   @brief    Run phd filter example.                                                              |
|   @author   Daniel Sturdivant <sturdivant20@gmail.com>                                           |
|   @date     May 2024                                                                             |
|                                                                                                  |
|==================================================================================================|
"""

import os
import numpy as np
import pandas as pd
from tqdm import tqdm
from pathlib import Path
from multiprocessing import pool, cpu_count, freeze_support
from scipy.linalg import block_diag

import navtools as nt
import charlizard.estimators.phd as phd
from charlizard.utils.combinations import combinations

PROJECT_PATH = Path(__file__).parents[2]
RESULTS_PATH = PROJECT_PATH / "results" / "mtt"
# DATAFILE = PROJECT_PATH / "data" / "mtt" / "multiple_traj.xlsx"
DATAFILE = PROJECT_PATH / "data" / "mtt" / "dynamic_data.xlsx"
# SCENARIO = ["multi_emitter_dynamic"]
SCENARIO = ["two_emitter_dynamic"]
# SCENARIO = ["single_emitter_dynamic"]
# SCENARIO = ["multi_emitter_static"]
# SCENARIO = ["two_emitter_static"]
# SCENARIO = ["single_emitter_static"]
N_RUNS = 100

aoa_std = np.deg2rad(2)
tdoa_std = 40e-9 * 299792458.0


def aoa_model(order: int, x: np.ndarray, rcvr_pos: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    dE = x[0] - rcvr_pos[:, 0]
    dN = x[1] - rcvr_pos[:, 1]
    y = nt.wrapTo2Pi(np.arctan2(dE, dN))
    r2 = dE**2 + dN**2

    if order == 2:
        Z = np.zeros((y.size, 2))
    elif order == 3:
        Z = np.zeros((y.size, 4))
    H = np.column_stack((dN, -dE, Z)) / r2[:, None]
    R = np.diag(aoa_std**2 * np.ones(y.size))
    # R_sqrt = np.diag(aoa_std * np.ones(y.size))
    return y, H, R  # , R_sqrt


def rdoa_model(order: int, x: np.ndarray, rcvr_pos: np.ndarray):
    idx = np.arange(rcvr_pos.shape[0])
    c0, c1 = combinations(idx)
    dR = x - rcvr_pos
    R = np.sqrt(dR[:, 0] ** 2 + dR[:, 1] ** 2)
    y = R[c0] - R[c1]

    if order == 2:
        Z = np.zeros((y.size, 2))
    elif order == 3:
        Z = np.zeros((y.size, 4))
    H = dR[c0, :] / R[c0, None] - dR[c1, :] / R[c1, None]
    H = np.column_stack((H, Z))
    R = np.diag(tdoa_std**2 * np.ones(y.size))
    # R_sqrt = np.diag(tdoa_std * np.ones(y.size))
    return y, H, R  # , R_sqrt


def aoa_rdoa_model(order: int, x: np.ndarray, rcvr_pos: np.ndarray):
    y1, H1, R1 = aoa_model(order, x, rcvr_pos)
    y2, H2, R2 = rdoa_model(order, x, rcvr_pos)
    y = np.concatenate((y1, y2))
    H = np.concatenate((H1, H2))
    R = block_diag(R1, R2)
    # R_sqrt = block_diag(R1_sqrt, R2_sqrt)
    return y, H, R  # , R_sqrt


# * ##### generate_scenario #####
def generate_scenario(scenario: str):
    # r = pd.read_excel(DATAFILE, sheet_name="rcvr").to_numpy()
    # e1 = pd.read_excel(DATAFILE, sheet_name="emit1").to_numpy()
    # e2 = pd.read_excel(DATAFILE, sheet_name="emit2").to_numpy()
    # e3 = pd.read_excel(DATAFILE, sheet_name="emit3").to_numpy()
    r = pd.read_excel(DATAFILE, sheet_name="Sheet1").to_numpy()
    e3 = pd.read_excel(DATAFILE, sheet_name="Sheet2").to_numpy()
    e2 = pd.read_excel(DATAFILE, sheet_name="Sheet3").to_numpy()
    e1 = pd.read_excel(DATAFILE, sheet_name="Sheet3").to_numpy()

    rcvr_pos = r[:, :2]
    rcvr_vel = r[:, 2:]
    N_rcvr = r.shape[0]

    N_time = int(max([e1[-1, 0] - e1[0, 0], e2[-1, 0] - e2[0, 0], e3[-1, 0] - e3[0, 0]]))
    rcvr_pos = [rcvr_pos] * N_time
    rcvr_vel = [rcvr_vel] * N_time

    if scenario.casefold() == "single_emitter_static":
        emit_pos = e3[0, 1:3]
        emit_vel = np.array([0.0, 0.0])
        emit_pos = [emit_pos] * N_time
        emit_vel = [emit_vel] * N_time
        w_birth = [0.1]
        N_emit = [1] * N_time
        t_birth = [0]
        t_end = [N_time]

    elif scenario.casefold() == "single_emitter_dynamic":
        emit_pos = [None] * N_time
        emit_vel = [None] * N_time
        N_emit = [1] * N_time
        w_birth = [0.1]
        t_birth = [int(x) for x in [e3[0, 0] - 1]]
        t_end = [int(x) for x in [e3[-1, 0] - 1]]
        for i in range(t_birth[0], t_end[0]):
            emit_pos[i] = e3[i, 1:3]
            emit_vel[i] = e3[i, 3:5]

    elif scenario.casefold() == "two_emitter_static":
        emit_pos = [None] * N_time
        emit_vel = [None] * N_time
        N_emit = [None] * N_time
        w_birth = [0.1, 0.1]
        t_birth = [int(x) for x in [e3[0, 0] - 1, e2[0, 0] - 1]]
        t_end = [int(x) for x in [e3[-1, 0] - 1, e2[-1, 0] - 1]]
        for i in range(t_birth[0], t_end[0]):
            if (i >= t_birth[1]) and (i <= t_end[1]):
                emit_pos[i] = np.array([e3[0, 1:3], e2[0, 1:3]])
                emit_vel[i] = np.zeros((2, 2))
                N_emit[i] = 2
            else:
                emit_pos[i] = e3[0, 1:3]
                emit_vel[i] = np.zeros(2)
                N_emit[i] = 1

    elif scenario.casefold() == "two_emitter_dynamic":
        emit_pos = [None] * N_time
        emit_vel = [None] * N_time
        N_emit = [None] * N_time
        w_birth = [0.1, 0.1]
        t_birth = [int(x) for x in [e3[0, 0] - 1, e2[0, 0] - 1]]
        t_end = [int(x) for x in [e3[-1, 0] - 1, e2[-1, 0] - 1]]
        for i in range(t_birth[0], t_end[0]):
            if (i >= t_birth[1]) and (i <= t_end[1]):
                emit_pos[i] = np.array([e3[i, 1:3], e2[i - t_birth[1], 1:3]])
                emit_vel[i] = np.array([e3[i, 3:5], e2[i - t_birth[1], 3:5]])
                N_emit[i] = 2
            else:
                emit_pos[i] = e3[i, 1:3]
                emit_vel[i] = e3[i, 3:5]
                N_emit[i] = 1

    elif scenario.casefold() == "multi_emitter_static":
        emit_pos = [None] * N_time
        emit_vel = [None] * N_time
        N_emit = [None] * N_time
        w_birth = [0.1, 0.1, 0.1]
        t_birth = [int(x) for x in [e3[0, 0] - 1, e2[0, 0] - 1, e1[0, 0] - 1]]
        t_end = [int(x) for x in [e3[-1, 0] - 1, e2[-1, 0] - 1, e1[-1, 0] - 1]]
        for i in range(t_birth[0], t_end[0]):
            emit_pos[i] = e3[0, 1:3]
            emit_vel[i] = np.zeros(2)
            N_emit[i] = 1
            if (i >= t_birth[1]) and (i <= t_end[1]):
                emit_pos[i] = np.vstack((emit_pos[i], e2[0, 1:3]))
                emit_vel[i] = np.vstack((emit_vel[i], np.zeros(2)))
                N_emit[i] += 1
            if (i >= t_birth[2]) and (i <= t_end[2]):
                emit_pos[i] = np.vstack((emit_pos[i], e1[0, 1:3]))
                emit_vel[i] = np.vstack((emit_vel[i], np.zeros(2)))
                N_emit[i] += 1

    elif scenario.casefold() == "multi_emitter_dynamic":
        emit_pos = [None] * N_time
        emit_vel = [None] * N_time
        N_emit = [None] * N_time
        w_birth = [0.1, 0.1, 0.1]
        t_birth = [int(x) for x in [e3[0, 0] - 1, e2[0, 0] - 1, e1[0, 0] - 1]]
        t_end = [int(x) for x in [e3[-1, 0] - 1, e2[-1, 0] - 1, e1[-1, 0] - 1]]
        for i in range(t_birth[0], t_end[0]):
            emit_pos[i] = e3[i, 1:3]
            emit_vel[i] = e3[i, 3:5]
            N_emit[i] = 1
            if (i >= t_birth[1]) and (i <= t_end[1]):
                emit_pos[i] = np.vstack((emit_pos[i], e2[i - t_birth[1], 1:3]))
                emit_vel[i] = np.vstack((emit_vel[i], e2[i - t_birth[1], 3:5]))
                N_emit[i] += 1
            if (i >= t_birth[2]) and (i <= t_end[2]):
                emit_pos[i] = np.vstack((emit_pos[i], e1[i - t_birth[2], 1:3]))
                emit_vel[i] = np.vstack((emit_vel[i], e1[i - t_birth[2], 3:5]))
                N_emit[i] += 1

    return phd.PHDFilterTruth(
        rcvr_pos=rcvr_pos,
        rcvr_vel=rcvr_vel,
        emit_pos=emit_pos,
        emit_vel=emit_vel,
        t_birth=t_birth,
        t_end=t_end,
        w_birth=w_birth,
        N_time=N_time,
        N_emit=N_emit,
        N_rcvr=N_rcvr,
    )


def run_phd_filter(x):
    scenario, run_i = x

    # generate truth
    Truth = generate_scenario(scenario)

    # create config
    Conf = phd.PHDFilterConfig(
        T=1.0,
        order=2,
        process_noise_std=1.5,
        meas_model=aoa_model,
        meas_clutter=2,
        meas_clutter_range=[0, 2 * np.pi],
        spawn_update_rate=20,
        p_d=0.98,
        p_s=0.99,
        prune_threshold=0.01,
        merge_threshold=2,
        cap_threshold=10,
    )

    # initialize filter
    filt = phd.PHDFilter(Conf)
    filt.gen_truth(Truth)
    filt.gen_meas()

    # x_init = np.array([[2.5, 2.5, 2.0, 2.0, 1.0, 1.0]]).reshape((6, 1))
    # P_init = np.array([[np.diag([50.0, 50.0, 5.0, 5.0, 1.0, 1.0])]]).reshape((6, 6, 1)) ** 2
    x_init = np.array([[2.5, 2.5, 2.0, 2.0]]).reshape((4, 1))
    P_init = np.array([[np.diag([50.0, 50.0, 5.0, 5.0])]]).reshape((4, 4, 1)) ** 2
    m_init = np.array([0.05])
    filt.run_filter(x_init, P_init, m_init)

    # --- turn results into numpy object arrays to save ---
    results = {
        "time": np.arange(min(Truth.t_birth), max(Truth.t_end)) * Conf.T,
        "x_true": np.empty(len(Truth.emit_pos), dtype=object),
        "n_true": np.empty(len(Truth.N_emit), dtype=object),
        "t_birth": np.array(Truth.t_birth),
        "t_death": np.array(Truth.t_end),
        "x_rcvr": np.hstack((Truth.rcvr_pos[0], Truth.rcvr_vel[0])).T,
        "x_est": np.empty(len(filt.Est.x), dtype=object),
        "P_est": np.empty(len(filt.Est.P), dtype=object),
        "w_est": np.empty(len(filt.Est.w), dtype=object),
        "n_est": np.empty(len(filt.Est.n), dtype=object),
        "y_meas": np.empty(len(filt.Y), dtype=object),
        "y_true": np.empty(len(filt.Y_true), dtype=object),
        "pos_err": np.empty(len(filt.Est.x_err), dtype=object),
    }
    for i in range(len(Truth.emit_pos)):
        results["x_true"][i] = np.hstack((Truth.emit_pos[i], Truth.emit_vel[i]))
        if len(results["x_true"][i].shape) == 1:
            results["x_true"][i] = np.array([results["x_true"][i]]).reshape(4, 1)
        else:
            results["x_true"][i] = results["x_true"][i].T
    results["n_true"][:] = Truth.N_emit
    results["x_est"][:] = filt.Est.x
    results["P_est"][:] = filt.Est.P
    results["w_est"][:] = filt.Est.w
    results["n_est"][:] = filt.Est.n
    results["y_meas"][:] = filt.Y
    results["y_true"][:] = filt.Y_true
    results["pos_err"][:] = filt.Est.x_err

    # --- save ---
    nt.io.ensure_exist(RESULTS_PATH / scenario)
    np.savez_compressed(RESULTS_PATH / scenario / f"run{run_i}", **results)
    # nt.io.savemat(RESULTS_PATH / scenario / "tyler_data.mat", results)
    return True


if __name__ == "__main__":
    freeze_support()
    # run_phd_filter(SCENARIO[0], 0)

    for scenario in SCENARIO:
        if os.path.isfile(RESULTS_PATH / scenario / "mc_results.npz"):
            os.remove(RESULTS_PATH / scenario / "mc_results.npz")
        prompt_string = f"[\u001b[31;1mcharlizard\u001b[0m] Monte Carlo for {scenario.upper()} "

        # run in parallel
        with pool.Pool(processes=cpu_count()) as p:
            args = [(scenario, i) for i in range(N_RUNS)]
            for b in tqdm(
                p.imap(run_phd_filter, args),
                total=N_RUNS,
                desc=prompt_string,
                ascii=".>#",
                bar_format="{desc}{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]",
                ncols=120,
            ):
                pass
