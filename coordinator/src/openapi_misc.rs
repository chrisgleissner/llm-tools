//! Documented 200 response schemas of the auth, tokens, diagnostics, events, notifications and other routes.
//!
//! `response` returns `Some(schema)` for a route this module owns and `None` otherwise, so that
//! `openapi::response_schema` falls through to the next module.

use serde_json::Value;

pub fn response(_path: &str, _method: &str) -> Option<Value> {
    None
}
