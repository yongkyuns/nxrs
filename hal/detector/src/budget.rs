mod generated {
    include!(concat!(env!("OUT_DIR"), "/execution_budget.rs"));
}
pub fn execution_budget() -> Option<crate::ExecutionBudget> {
    generated::BUDGET
}
