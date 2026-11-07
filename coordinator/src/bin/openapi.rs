fn main() {
    println!(
        "{}",
        serde_json::to_string_pretty(&bridge_coordinator::openapi::openapi_value()).expect("OpenAPI serializes")
    );
}
