# Copyright 2024 Advanced Micro Devices, Inc.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

# Given an input dispatch, this code modifies the hyperparameters
# in the code and runs it.

import z3  # type: ignore
from typing import Iterator

from .common import *


def get_mfma_intrinsic_constraints(
    problem_size: ProblemSize,
    intrinsic_m: z3.ArithRef,
    intrinsic_n: z3.ArithRef,
    intrinsic_k: z3.ArithRef,
) -> z3.BoolRef:
    compatible_intrinsics = get_compatible_mfma_intrinsics(problem_size)
    assert len(compatible_intrinsics) > 0, "No compatible intrinsics found"
    return z3.Or(
        *(
            z3.And(intrinsic_m == mfma.m, intrinsic_n == mfma.n, intrinsic_k == mfma.k)
            for mfma in compatible_intrinsics
        )
    )


def get_dispatch_constraints(
    problem_size: ProblemSize,
    tile_m: z3.ArithRef,
    tile_n: z3.ArithRef,
    tile_k: z3.ArithRef,
) -> list[z3.BoolRef]:
    if problem_size.dispatch_kind != DispatchKind.conv:
        return []

    dim_info = ConvDimInfo.from_problem_size(problem_size)
    conv_constraints = []
    # WARNING: This sometimes makes the constraints UNSAT for some reason.
    conv_constraints += [tile_m <= dim_info.ow]
    conv_constraints += [tile_n <= dim_info.oc]
    conv_constraints += [tile_k <= dim_info.ic]
    return conv_constraints


def calculate_shared_memory_usage_in_bytes(
    problem_size: ProblemSize,
    m: int | z3.ArithRef,
    n: int | z3.ArithRef,
    k: int | z3.ArithRef,
) -> int | z3.ArithRef:
    lhs_memory = m * k * (problem_size.lhs_type.bitwidth // 8)
    rhs_memory = k * n * (problem_size.rhs_type.bitwidth // 8)
    return lhs_memory + rhs_memory


def generate_constraints(
    problem_size: ProblemSize,
    tile_sizes,
    num_subgroups,
    subgroup_size,
    intrinsic_size,
    workgroup_size,
    subgroup_m_count,
    subgroup_n_count,
    waves_per_eu,
):
    M, N, K = (
        problem_size.matmul_size.M,
        problem_size.matmul_size.N,
        problem_size.matmul_size.K,
    )
    m, n, k = tile_sizes
    intrinsic_mn, intrinsic_k = intrinsic_size
    wg_x, wg_y, wg_z = workgroup_size
    wg_threads = z3.Int("wg_threads")
    constraints = [wg_threads == wg_x * wg_y * wg_z]
    constraints += [subgroup_size == 64, wg_threads <= 1024]
    constraints += [
        get_mfma_intrinsic_constraints(
            problem_size, intrinsic_mn, intrinsic_mn, intrinsic_k
        )
    ]
    subgroup_k_count = 1
    constraints += [
        m >= intrinsic_mn,
        m <= 512,
        m <= M,
    ]
    constraints += [n >= intrinsic_mn, n <= 512, n <= N, N % n == 0]
    constraints += [k >= intrinsic_k, k <= 512, k <= K, K % k == 0]
    for x in (subgroup_m_count, subgroup_n_count):
        constraints += [x >= 1, x <= 32]

    subgroup_m_tile_count = z3.Int("sg_m_tcnt")
    subgroup_n_tile_count = z3.Int("sg_n_tcnt")
    subgroup_k_tile_count = z3.Int("sg_k_tcnt")
    for x in (subgroup_m_tile_count, subgroup_n_tile_count, subgroup_k_tile_count):
        constraints += [x >= 1, x <= 32]

    constraints += [m == subgroup_m_count * subgroup_m_tile_count * intrinsic_mn]
    constraints += [n == subgroup_n_count * subgroup_n_tile_count * intrinsic_mn]
    constraints += [k == subgroup_k_count * subgroup_k_tile_count * intrinsic_k]
    constraints += [wg_x == subgroup_size * subgroup_n_count]
    constraints += [wg_y == subgroup_m_count]
    constraints += [wg_z == subgroup_k_count]
    constraints += [z3.Or(wg_x <= n, wg_x <= m)]
    constraints += [k % intrinsic_mn == 0]
    constraints += [(k * n) % wg_threads == 0]
    constraints += [(k * m) % wg_threads == 0]
    subgroups = subgroup_m_count * subgroup_n_count
    if num_subgroups > 0:
        constraints += [subgroups == num_subgroups]
    else:
        constraints += [subgroups >= 1, subgroups <= 10]

    constraints += [waves_per_eu == 2]
    # constraints += [z3.Or(waves_per_eu == 2, waves_per_eu == 3, waves_per_eu == 4)]

    shared_memory = calculate_shared_memory_usage_in_bytes(problem_size, m, n, k)
    constraints += [shared_memory <= 65536]

    constraints += get_dispatch_constraints(problem_size, m, n, k)

    return constraints


def generate_solutions(
    logger: logging.Logger, problem_size: ProblemSize, num_subgrups: int
) -> Iterator[Configuration]:
    M, N, K = problem_size.MNK
    logger.info(f"{M},{N},{K}")
    m, n, k = z3.Int("m"), z3.Int("n"), z3.Int("k")
    subgroup_size = z3.Int("subgroup_size")
    intrinsic_mn = z3.Int("intrinsic_mn")
    intrinsic_k = z3.Int("intrinsic_k")
    wg_x, wg_y, wg_z = z3.Int("wg_x"), z3.Int("wg_y"), z3.Int("wg_z")
    sg_m_cnt = z3.Int("sg_m_cnt")
    sg_n_cnt = z3.Int("sg_n_cnt")
    waves_per_eu = z3.Int("waves_per_eu")
    all_vars = [
        m,
        n,
        k,
        subgroup_size,
        intrinsic_mn,
        intrinsic_k,
        wg_x,
        wg_y,
        wg_z,
        sg_m_cnt,
        sg_n_cnt,
        waves_per_eu,
    ]

    solver = z3.Solver()
    constraints = generate_constraints(
        problem_size,
        [m, n, k],
        num_subgrups,
        subgroup_size,
        [intrinsic_mn, intrinsic_k],
        [wg_x, wg_y, wg_z],
        sg_m_cnt,
        sg_n_cnt,
        waves_per_eu,
    )
    solver.add(z3.simplify(z3.And(constraints)))
    logger.debug(f"Initial constraints: {solver}")
    i = 0
    while solver.check() == z3.sat:
        model = solver.model()
        lookup = lambda var: model[var].as_long()

        config = Configuration(
            lookup(subgroup_size),
            [lookup(wg_x), lookup(wg_y), lookup(wg_z)],
            MfmaIntrinsic(
                problem_size.res_type.element_type,
                lookup(intrinsic_mn),
                lookup(intrinsic_mn),
                lookup(intrinsic_k),
                problem_size.lhs_type.element_type,
            ),
            [lookup(m), lookup(n), lookup(k)],
            lookup(sg_m_cnt),
            lookup(sg_n_cnt),
            GpuPipelineOptions(),
            lookup(waves_per_eu),
        )
        solver.add(z3.simplify(z3.Not(z3.And(list(x == model[x] for x in all_vars)))))
        i += 1
        yield config
