use lock_order::scope;
fn main() {
    let escaped = scope(|root| root);
    let _ = escaped;
}
