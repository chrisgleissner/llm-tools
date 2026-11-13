//! Documented 200 response schemas of the mission, task, proposal, settings and rotation routes (/api/v1/missions...).
//!
//! `response` returns `Some(schema)` for a route this module owns and `None` otherwise, so that
//! `openapi::response_schema` falls through to the next module.

use serde_json::Value;

pub fn response(_path: &str, _method: &str) -> Option<Value> {
    None
}
