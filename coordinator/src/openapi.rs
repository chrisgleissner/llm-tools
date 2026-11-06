//! OpenAPI document generated from the coordinator route policy table and Rust response types.

use crate::http::{Access, MAX_JSON_ARRAY_ITEMS, MAX_JSON_DEPTH, MAX_JSON_OBJECT_FIELDS, MAX_JSON_STRING_BYTES, ROUTES};
use axum::response::IntoResponse;
use serde::Serialize;
use serde_json::{json, Map, Value};
use utoipa::OpenApi;

#[derive(Serialize, utoipa::ToSchema)]
struct ErrorDetail {
    code: String,
    message: String,
}

#[derive(Serialize, utoipa::ToSchema)]
struct ProblemDetails {
    #[serde(rename = "type")]
    #[schema(rename = "type")]
    type_uri: String,
    title: String,
    status: u16,
    detail: String,
    code: String,
    error: ErrorDetail,
}

#[derive(Serialize, utoipa::ToSchema)]
struct CommandOutcome {
    request_id: String,
    target: String,
    mutating: bool,
    #[schema(value_type = Object)]
    request: Value,
    state: String,
    operation: String,
    #[schema(value_type = Option<Object>)]
    result: Option<Value>,
    created_at: i64,
    updated_at: i64,
}

#[derive(Serialize, utoipa::ToSchema)]
struct TokenInfo {
    name: String,
    scopes: Vec<String>,
    created_at: String,
    last_used: Option<String>,
    expires_at: Option<String>,
}

#[derive(Serialize, utoipa::ToSchema)]
struct TokenList {
    tokens: Vec<TokenInfo>,
}

#[derive(Serialize, utoipa::ToSchema)]
struct TokenCreate {
    name: String,
    scopes: Option<Vec<String>>,
    expires_days: Option<u32>,
}

#[derive(Serialize, utoipa::ToSchema)]
struct TokenCreated {
    token: TokenInfo,
    secret: String,
}

#[derive(OpenApi)]
#[openapi(components(schemas(ErrorDetail, ProblemDetails, CommandOutcome, TokenInfo, TokenList, TokenCreate, TokenCreated)))]
struct ApiSchemas;

fn operation_id(path: &str, method: &str) -> String {
    let tail = path.strip_prefix("/api/v1/").unwrap_or(path);
    let mut id = method.to_ascii_lowercase();
    for part in tail.split('/') {
        if part.starts_with('{') {
            id.push_str("_by_");
            id.push_str(part.trim_matches(['{', '}']));
        } else {
            id.push('_');
            id.push_str(&part.replace('.', "_"));
        }
    }
    id
}

fn response_schema(path: &str, method: &str) -> Value {
    if path == "/api/v1/missions" && method == "GET" {
        return json!({"$ref":"#/components/schemas/MissionListPage"});
    }
    if path == "/api/v1/tokens" && method == "GET" {
        return json!({"$ref":"#/components/schemas/TokenList"});
    }
    if path == "/api/v1/tokens" && method == "POST" {
        return json!({"$ref":"#/components/schemas/TokenCreated"});
    }
    if path == "/api/v1/commands/{id}" || path == "/api/v1/commands" {
        json!({"$ref":"#/components/schemas/CommandOutcome"})
    } else {
        json!({"type":"object","additionalProperties":true})
    }
}

fn operation(path: &str, method: &str, access: Access, scope: &str) -> Value {
    let mut op = json!({
        "operationId": operation_id(path, method),
        "summary": format!("{} {}", method.to_ascii_uppercase(), path),
        "responses": {
            "200": {"description":"Successful response","content":{"application/json":{"schema":response_schema(path, method)}}},
            "400": {"description":"Invalid request","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "401": {"description":"Authentication required","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "403": {"description":"Insufficient scope or forbidden request","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "404": {"description":"Resource not found","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "412": {"description":"The supplied task revision is stale","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "415": {"description":"Mutation body is not application/json","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "405": {"description":"Method not allowed","headers":{"Allow":{"schema":{"type":"string"}}},"content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "500": {"description":"Internal server error","content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}},
            "429": {"description":"Rate limited","headers":{"Retry-After":{"schema":{"type":"integer"}}},"content":{"application/problem+json":{"schema":{"$ref":"#/components/schemas/ProblemDetails"}}}}
        }
    });
    if path == "/api/v1/tokens" && method == "POST" {
        op["requestBody"] = json!({
            "required":true,
            "content":{"application/json":{"schema":{"$ref":"#/components/schemas/TokenCreate"}}}
        });
        op["responses"]["201"] = json!({
            "description":"Token created; secret is returned once",
            "content":{"application/json":{"schema":{"$ref":"#/components/schemas/TokenCreated"}}}
        });
    }
    if path == "/api/v1/commands" {
        op["parameters"] = json!([{
            "name":"Idempotency-Key",
            "in":"header",
            "required":false,
            "description":"When supplied, this value is used as the durable command request_id.",
            "schema":{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9._-]+$"}
        }]);
        op["responses"]["202"] = json!({
            "description":"Command is queued or has not completed",
            "headers":{"Location":{"schema":{"type":"string","format":"uri-reference"}}},
            "content":{"application/json":{"schema":{"$ref":"#/components/schemas/CommandOutcome"}}}
        });
    }
    if path == "/api/v1/missions" && method == "GET" {
        op["parameters"] = json!([
            {"name":"q","in":"query","required":false,"schema":{"type":"string","maxLength":1024}},
            {"name":"worker","in":"query","required":false,"schema":{"type":"string","maxLength":63}},
            {"name":"status","in":"query","required":false,"schema":{"type":"string","maxLength":256}},
            {"name":"sort","in":"query","required":false,"schema":{"type":"string","enum":["name","status","worker","updated"]}},
            {"name":"order","in":"query","required":false,"schema":{"type":"string","enum":["asc","desc"]}},
            {"name":"limit","in":"query","required":false,"description":"Page size, default 100, maximum 500.","schema":{"type":"integer","minimum":1,"maximum":500}},
            {"name":"cursor","in":"query","required":false,"description":"Authenticated and encrypted opaque cursor returned as next_cursor; bound to this filter set.","schema":{"type":"string","maxLength":2048}},
            {"name":"offset","in":"query","required":false,"deprecated":true,"description":"Legacy pagination; use cursor.","schema":{"type":"integer","minimum":0}}
        ]);
    }
    if method == "POST" || method == "PATCH" || method == "DELETE" {
        op["requestBody"] = json!({
            "required": method == "POST",
            "content":{"application/json":{"schema":{"$ref":"#/components/schemas/BoundedRequestObject"}}}
        });
    }
    if path == "/api/v1/tokens" && method == "POST" {
        op["requestBody"] = json!({
            "required":true,
            "content":{"application/json":{"schema":{"$ref":"#/components/schemas/TokenCreate"}}}
        });
    }
    if let Some(params) = op.get_mut("parameters").and_then(Value::as_array_mut) {
        for segment in path.split('/') {
            if segment.starts_with('{') && segment.ends_with('}') {
                params.push(json!({"name":segment.trim_matches(['{','}']),"in":"path","required":true,"schema":{"type":"string"}}));
            }
        }
    } else if path.contains('{') {
        op["parameters"] = Value::Array(
            path.split('/')
                .filter(|segment| segment.starts_with('{') && segment.ends_with('}'))
                .map(|segment| {
                    json!({
                        "name":segment.trim_matches(['{','}']),"in":"path","required":true,"schema":{"type":"string"}
                    })
                })
                .collect(),
        );
    }
    if path == "/api/v1/missions/{id}" || path.starts_with("/api/v1/missions/{id}/") {
        let mut params = op["parameters"].as_array().cloned().unwrap_or_default();
        params.push(json!({
            "name":"worker","in":"query","required":true,
            "description":"Worker that owns this mission ID.",
            "schema":{"type":"string","minLength":1,"maxLength":63,"pattern":"^[a-z0-9][a-z0-9-]*$"}
        }));
        if method == "GET" {
            params.push(json!({
                "name":"wait_ms","in":"query","required":false,
                "description":"Wait for the worker response for up to 30 seconds; otherwise return its durable pending command.",
                "schema":{"type":"integer","minimum":0,"maximum":30000}
            }));
        }
        if method == "PATCH" || path.ends_with("/verify") || path.ends_with("/{action}") {
            params.push(json!({
                "name":"If-Match","in":"header","required":false,
                "description":"Task revision ETag returned by GET /missions/{id}/tasks/{n}; maps to expected_revision.",
                "schema":{"type":"string","pattern":"^\\\"[0-9]+\\\"$"}
            }));
        }
        if path == "/api/v1/missions/{id}/tasks/{n}" && method == "GET" {
            op["responses"]["200"]["headers"] = json!({
                "ETag":{"description":"Strong task revision tag, when the worker answered synchronously.","schema":{"type":"string","pattern":"^\\\"[0-9]+\\\"$"}}
            });
        }
        if method == "GET"
            && matches!(
                path,
                "/api/v1/missions/{id}/tasks" | "/api/v1/missions/{id}/events" | "/api/v1/missions/{id}/operations"
            )
        {
            let page_key = match path {
                "/api/v1/missions/{id}/events" => "after",
                "/api/v1/missions/{id}/tasks" => "offset",
                _ => "",
            };
            if !page_key.is_empty() {
                params.push(json!({
                    "name":page_key,"in":"query","required":false,
                    "schema":{"type":"integer","minimum":0}
                }));
            }
            params.push(json!({
                "name":"limit","in":"query","required":false,
                "schema":{"type":"integer","minimum":1,"maximum":1000}
            }));
        }
        op["parameters"] = Value::Array(params);
    }
    if access == Access::Authed {
        op["security"] = json!([{"bearerAuth":[]}, {"cookieSession":[]}]);
        op["x-required-scope"] = if scope == "terminal:conditional" {
            json!(["terminal:view", "terminal:write"])
        } else {
            json!([scope])
        };
    } else if access == Access::WorkerToken {
        op["security"] = json!([{"workerBearer":[]}]);
    } else {
        op["security"] = json!([]);
    }
    op
}

fn bounded_value_schema(depth: usize) -> Value {
    let mut choices = vec![
        json!({"type":"string","maxLength":MAX_JSON_STRING_BYTES}),
        json!({"type":"number"}),
        json!({"type":"integer"}),
        json!({"type":"boolean"}),
        json!({"type":"null"}),
    ];
    if depth < MAX_JSON_DEPTH {
        let next = format!("#/components/schemas/BoundedJsonValueDepth{}", depth + 1);
        choices.push(json!({"type":"array","maxItems":MAX_JSON_ARRAY_ITEMS,"items":{"$ref":next}}));
        choices.push(json!({"type":"object","maxProperties":MAX_JSON_OBJECT_FIELDS,"propertyNames":{"maxLength":128},"additionalProperties":{"$ref":next}}));
    }
    json!({"anyOf":choices})
}

/// Build a stable OpenAPI 3.1 document from the same table used for auth policy enumeration.
pub fn openapi_value() -> Value {
    let mut doc = serde_json::to_value(ApiSchemas::openapi()).expect("OpenAPI schema serializes");
    doc["openapi"] = json!("3.1.0");
    doc["info"] = json!({
        "title":"LLM Bridge Coordinator API",
        "version":"v1",
        "license":{"name":"Apache-2.0","url":"https://www.apache.org/licenses/LICENSE-2.0"},
        "description":"Versioned coordinator API. Additive changes are made within v1; breaking changes require v2."
    });
    doc["servers"] = json!([{"url":"/"}]);
    doc["components"]["securitySchemes"] = json!({
        "bearerAuth":{"type":"http","scheme":"bearer","bearerFormat":"LLM Bridge API token"},
        "cookieSession":{"type":"apiKey","in":"cookie","name":"__Host-bridge_session"},
        "workerBearer":{"type":"http","scheme":"bearer","bearerFormat":"Worker token"}
    });
    doc["components"]["schemas"]["BoundedRequestObject"] = json!({
        "type":"object",
        "maxProperties":MAX_JSON_OBJECT_FIELDS,
        "propertyNames":{"maxLength":128},
        "additionalProperties":{"$ref":"#/components/schemas/BoundedJsonValueDepth0"},
        "description":format!("JSON is limited to {MAX_JSON_DEPTH} nested levels, strings of at most {MAX_JSON_STRING_BYTES} UTF-8 bytes, arrays of at most {MAX_JSON_ARRAY_ITEMS} items and objects of at most {MAX_JSON_OBJECT_FIELDS} fields.")
    });
    doc["components"]["schemas"]["MissionListPage"] = json!({
        "type":"object",
        "required":["missions","total","offset","next_cursor","statuses"],
        "properties":{
            "missions":{"type":"array","items":{"type":"object","required":["worker","stale","mission"],"properties":{"worker":{"type":"string"},"stale":{"type":"boolean"},"mission":{"type":"object","additionalProperties":true}}}},
            "total":{"type":"integer","minimum":0},
            "offset":{"type":"integer","minimum":0,"description":"Compatibility field; use next_cursor for subsequent pages."},
            "next_cursor":{"type":["string","null"],"maxLength":2048},
            "statuses":{"type":"object","additionalProperties":{"type":"integer","minimum":0}}
        }
    });
    for depth in 0..=MAX_JSON_DEPTH {
        doc["components"]["schemas"][format!("BoundedJsonValueDepth{depth}")] = bounded_value_schema(depth);
    }
    let mut paths = Map::new();
    for (path, methods, access, scope) in ROUTES {
        let item = paths.entry((*path).to_string()).or_insert_with(|| json!({}));
        for method in *methods {
            item[method.to_ascii_lowercase()] = operation(path, method, *access, scope);
        }
    }
    doc["paths"] = Value::Object(paths);
    doc
}

pub async fn document() -> impl IntoResponse {
    axum::Json(openapi_value())
}

/// Small self-hosted reference page. It contains no scripts, remote assets or inline styling.
pub async fn reference() -> impl IntoResponse {
    let doc = openapi_value();
    let mut rows = String::new();
    if let Some(paths) = doc.get("paths").and_then(Value::as_object) {
        for (path, item) in paths {
            if let Some(operations) = item.as_object() {
                for (method, operation) in operations {
                    let summary = operation.get("operationId").and_then(Value::as_str).unwrap_or("API operation");
                    rows.push_str(&format!(
                        "<tr><td>{}</td><td><code>{}</code></td><td><code>{}</code></td></tr>",
                        method.to_ascii_uppercase(),
                        path,
                        summary
                    ));
                }
            }
        }
    }
    let html = format!("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Coordinator API</title><main><h1>Coordinator API</h1><p>OpenAPI 3.1 document: <a href=\"/api/v1/openapi.json\">/api/v1/openapi.json</a></p><table><thead><tr><th>Method</th><th>Path</th><th>Operation</th></tr></thead><tbody>{rows}</tbody></table></main></html>");
    ([(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")], html)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn openapi_is_31_and_covers_every_declared_route_and_scope() {
        let doc = openapi_value();
        assert_eq!(doc["openapi"], "3.1.0");
        let paths = doc["paths"].as_object().expect("paths object");
        for (path, methods, access, scope) in ROUTES {
            let item = paths.get(*path).expect("route present in OpenAPI");
            for method in *methods {
                let operation = &item[method.to_ascii_lowercase()];
                assert!(!operation.is_null(), "{method} {path} missing");
                if *access == Access::Authed {
                    let expected = if *scope == "terminal:conditional" {
                        json!(["terminal:view", "terminal:write"])
                    } else {
                        json!([*scope])
                    };
                    assert_eq!(operation["x-required-scope"], expected);
                    assert!(operation["security"][0]["bearerAuth"].as_array().unwrap().is_empty());
                } else if *access == Access::WorkerToken {
                    assert!(operation["security"][0]["workerBearer"].is_array());
                }
                if matches!(*method, "POST" | "PATCH") {
                    assert!(operation["responses"]["415"]["content"]["application/problem+json"].is_object());
                }
            }
        }
        for path in paths.keys() {
            assert!(ROUTES.iter().any(|(declared, _, _, _)| declared == path));
        }
        assert!(doc["components"]["schemas"]["ProblemDetails"].is_object());
        let mission_list = &doc["paths"]["/api/v1/missions"]["get"];
        let params = mission_list["parameters"].as_array().expect("mission list query parameters");
        let cursor = params
            .iter()
            .find(|param| param["name"] == "cursor" && param["in"] == "query")
            .expect("opaque cursor query parameter");
        assert_eq!(cursor["schema"]["type"], "string");
        assert_eq!(cursor["schema"]["maxLength"], 2048);
        assert!(cursor["description"].as_str().unwrap().contains("bound to this filter set"));
        assert_eq!(
            mission_list["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/MissionListPage"
        );
        assert_eq!(
            doc["components"]["schemas"]["MissionListPage"]["properties"]["next_cursor"]["type"],
            json!(["string", "null"])
        );
    }

    #[test]
    fn generated_openapi_matches_committed_snapshot() {
        let snapshot: Value = serde_json::from_str(include_str!("../../docs/distributed/openapi.json")).expect("valid snapshot");
        assert_eq!(openapi_value(), snapshot);
    }
}
