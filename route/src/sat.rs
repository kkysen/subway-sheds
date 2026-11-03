//! A CP-SAT model for Python to build and solve, through OR-Tools' C++ library.
//!
//! Just what scheduling trains needs: integer and boolean variables,
//! linear constraints that may be enforced by literals,
//! fixed-size intervals, and no-overlap and cumulative constraints on them.
//! Variables, literals, and intervals are indices, as CP-SAT's own protos have them,
//! with a literal's negation `-i - 1` (`~i` in Python).

use cp_sat::proto::constraint_proto::Constraint;
use cp_sat::proto::{
    ConstraintProto, CpModelProto, CpObjectiveProto, CpSolverStatus, CumulativeConstraintProto,
    IntegerVariableProto, IntervalConstraintProto, LinearConstraintProto, LinearExpressionProto,
    NoOverlapConstraintProto, SatParameters,
};
use pyo3::exceptions::PyValueError;
use pyo3::{PyResult, Python, pyclass, pymethods};

/// A linear expression from Python: `(variable, coefficient)` terms.
type Terms = Vec<(i32, i64)>;

fn expr(terms: &[(i32, i64)], offset: i64) -> LinearExpressionProto {
    LinearExpressionProto {
        vars: terms.iter().map(|&(v, _)| v).collect(),
        coeffs: terms.iter().map(|&(_, c)| c).collect(),
        offset,
    }
}

fn constant(value: i64) -> LinearExpressionProto {
    expr(&[], value)
}

#[pyclass(name = "CpModel", module = "subway_sheds._route")]
#[derive(Default)]
pub struct Model {
    proto: CpModelProto,
}

impl Model {
    fn add(&mut self, constraint: Constraint, enforce: Vec<i32>) -> i32 {
        let index = self.proto.constraints.len();
        self.proto.constraints.push(ConstraintProto {
            enforcement_literal: enforce,
            constraint: Some(constraint),
            ..Default::default()
        });
        index.try_into().expect("fewer than 2^31 constraints")
    }
}

#[pymethods]
impl Model {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    /// A new integer variable, from `lo` to `hi`.
    fn new_int(&mut self, lo: i64, hi: i64) -> PyResult<i32> {
        if lo > hi {
            return Err(PyValueError::new_err(format!("empty domain {lo}..={hi}")));
        }
        let index = self.proto.variables.len();
        self.proto.variables.push(IntegerVariableProto {
            domain: vec![lo, hi],
            ..Default::default()
        });
        Ok(index.try_into().expect("fewer than 2^31 variables"))
    }

    /// A new boolean variable, usable as a literal.
    fn new_bool(&mut self) -> PyResult<i32> {
        self.new_int(0, 1)
    }

    /// `lo <= terms <= hi`, only where every literal of `enforce` is true.
    #[pyo3(signature = (terms, lo, hi, enforce = Vec::new()))]
    fn add_linear(&mut self, terms: Terms, lo: i64, hi: i64, enforce: Vec<i32>) {
        let linear = LinearConstraintProto {
            vars: terms.iter().map(|&(v, _)| v).collect(),
            coeffs: terms.iter().map(|&(_, c)| c).collect(),
            domain: vec![lo, hi],
        };
        self.add(Constraint::Linear(linear), enforce);
    }

    /// An interval starting at `terms + offset`, `size` long.
    fn new_interval(&mut self, terms: Terms, offset: i64, size: i64) -> i32 {
        let interval = IntervalConstraintProto {
            start: Some(expr(&terms, offset)),
            end: Some(expr(&terms, offset + size)),
            size: Some(constant(size)),
        };
        self.add(Constraint::Interval(interval), Vec::new())
    }

    /// No two of `intervals` overlap.
    fn add_no_overlap(&mut self, intervals: Vec<i32>) {
        self.add(
            Constraint::NoOverlap(NoOverlapConstraintProto { intervals }),
            Vec::new(),
        );
    }

    /// At most `capacity` of `intervals` overlap at any time.
    fn add_cumulative(&mut self, intervals: Vec<i32>, capacity: i64) {
        let demands = intervals.iter().map(|_| constant(1)).collect();
        let cumulative = CumulativeConstraintProto {
            capacity: Some(constant(capacity)),
            intervals,
            demands,
        };
        self.add(Constraint::Cumulative(cumulative), Vec::new());
    }

    /// Minimize `terms`.
    fn minimize(&mut self, terms: Terms) {
        self.proto.objective = Some(CpObjectiveProto {
            vars: terms.iter().map(|&(v, _)| v).collect(),
            coeffs: terms.iter().map(|&(_, c)| c).collect(),
            ..Default::default()
        });
    }

    /// The model's size, as CP-SAT describes it.
    fn stats(&self) -> String {
        cp_sat::ffi::cp_model_stats(&self.proto)
    }

    /// Solves the model in at most `seconds`, or `deterministic` of CP-SAT's own deterministic time,
    /// on `workers` threads, from `seed`.
    /// Returns the status (`"OPTIMAL"`, `"FEASIBLE"`, `"INFEASIBLE"`, `"UNKNOWN"`),
    /// the objective, its best bound, the wall time, and every variable's value,
    /// empty if there's no solution.
    #[pyo3(signature = (seconds, deterministic, workers, seed = 0, log = false))]
    fn solve(
        &self,
        py: Python<'_>,
        seconds: f64,
        deterministic: f64,
        workers: i32,
        seed: i32,
        log: bool,
    ) -> PyResult<(&'static str, f64, f64, f64, Vec<i64>)> {
        let invalid = cp_sat::ffi::validate_cp_model(&self.proto);
        if !invalid.is_empty() {
            return Err(PyValueError::new_err(invalid));
        }
        let params = SatParameters {
            max_time_in_seconds: Some(seconds),
            max_deterministic_time: Some(deterministic),
            num_workers: Some(workers),
            random_seed: Some(seed),
            log_search_progress: Some(log),
            ..Default::default()
        };
        let response = py.detach(|| cp_sat::ffi::solve_with_parameters(&self.proto, &params));
        let status = match CpSolverStatus::from_i32(response.status) {
            Some(CpSolverStatus::Optimal) => "OPTIMAL",
            Some(CpSolverStatus::Feasible) => "FEASIBLE",
            Some(CpSolverStatus::Infeasible) => "INFEASIBLE",
            Some(CpSolverStatus::Unknown) => "UNKNOWN",
            _ => return Err(PyValueError::new_err("CP-SAT found the model invalid")),
        };
        Ok((
            status,
            response.objective_value,
            response.best_objective_bound,
            response.wall_time,
            response.solution,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Three trains on one track, each wanting to leave at 0, 1 apart: they spread to 0, 1, 2.
    #[test]
    fn spreads_trains_on_a_track() {
        Python::initialize();
        Python::attach(|py| {
            let mut m = Model::new();
            let deps: Vec<i32> = (0..3).map(|_| m.new_int(0, 10).unwrap()).collect();
            let intervals = deps
                .iter()
                .map(|&d| m.new_interval(vec![(d, 1)], 0, 1))
                .collect();
            m.add_no_overlap(intervals);
            m.minimize(deps.iter().map(|&d| (d, 1)).collect());
            let (status, objective, ..) = m.solve(py, 10.0, 10.0, 1, 0, false).unwrap();
            assert_eq!(status, "OPTIMAL");
            assert_eq!(objective, 3.0);
        });
    }

    /// An enforced constraint only holds where its literal does.
    #[test]
    fn enforces_by_literal() {
        Python::initialize();
        Python::attach(|py| {
            let mut m = Model::new();
            let x = m.new_int(0, 10).unwrap();
            let b = m.new_bool().unwrap();
            m.add_linear(vec![(x, 1)], 5, 10, vec![b]);
            m.add_linear(vec![(x, 1)], 0, 2, vec![!b]);
            m.add_linear(vec![(b, 1)], 1, 1, Vec::new());
            m.minimize(vec![(x, 1)]);
            let (status, _, _, _, values) = m.solve(py, 10.0, 10.0, 1, 0, false).unwrap();
            assert_eq!(status, "OPTIMAL");
            assert_eq!(values[x as usize], 5);
        });
    }
}
