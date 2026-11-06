//! AUTH-001: every route the coordinator serves, requested without credentials.
mod common;
use bridge_coordinator::http::{Access, ROUTES};
use common::*;
use reqwest::Method;
use std::collections::BTreeSet;

/// Paths of the `.route("...")` calls inside `router()`, read from the production source.
fn routed_paths() -> BTreeSet<String> {
    let src = include_str!("../src/http.rs");
    let body = &src[src.find("pub fn router(").expect("router fn")..];
    let body = &body[..body.find(".fallback(").expect("router fallback")];
    body.split(".route(\"")
        .skip(1)
        .map(|rest| rest[..rest.find('"').unwrap()].to_string())
        .collect()
}

fn concrete(path: &str) -> String {
    path.replace("{name}", "w1")
        .replace("{id}", "x")
        .replace("{session_id}", "s1")
        .replace("{action}", "pause")
        .replace("{n}", "1")
}

fn refused_with(err: tokio_tungstenite::tungstenite::Error, status: u16) {
    match err {
        tokio_tungstenite::tungstenite::Error::Http(r) => assert_eq!(r.status().as_u16(), status),
        other => panic!("expected HTTP {status}, got {other:?}"),
    }
}

#[test]
fn route_table_matches_the_router_definition() {
    let routes: BTreeSet<(String, String)> = ROUTES
        .iter()
        .flat_map(|(path, methods, _, _)| methods.iter().map(|method| (path.to_string(), (*method).to_string())))
        .collect();
    let declared_count: usize = ROUTES.iter().map(|(_, methods, _, _)| methods.len()).sum();
    assert_eq!(routes.len(), declared_count, "duplicate path/method policy in ROUTES");
    let table: BTreeSet<String> = ROUTES.iter().map(|(p, _, _, _)| p.to_string()).collect();
    assert_eq!(
        routed_paths(),
        table,
        "http::ROUTES and the .route(...) calls of http::router must list the same paths"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn every_route_requires_credentials_except_the_public_ones() {
    let env = boot().await;
    let anon = env.api();
    let mut seen_public = Vec::new();
    for (path, methods, access, _) in ROUTES {
        let url = concrete(path);
        for m in *methods {
            let method: Method = m.parse().unwrap();
            match access {
                Access::Authed => {
                    let (st, body) = anon.send(anon.bare(method.clone(), &url)).await;
                    assert_eq!(st, 401, "{m} {url} without credentials: {body}");
                    let (st, _) = anon.send(anon.bare(method.clone(), &url).header("authorization", "Bearer nope")).await;
                    assert_eq!(st, 401, "{m} {url} with a bad bearer token");
                    let (st, _) = anon.send(anon.bare(method, &url).header("cookie", "__Host-bridge_session=nope")).await;
                    assert_eq!(st, 401, "{m} {url} with a bad session cookie");
                    if url.ends_with("/stream") || url.ends_with("/terminal") {
                        refused_with(env.ws(&url, &[]).await.expect_err("no credentials"), 401);
                        refused_with(env.ws(&url, &[("authorization", "Bearer nope")]).await.expect_err("bad token"), 401);
                    }
                }
                Access::WorkerToken => {
                    refused_with(env.ws(&url, &[]).await.expect_err("no worker token"), 401);
                    refused_with(
                        env.ws(&url, &[("authorization", "Bearer not-a-worker-token")])
                            .await
                            .expect_err("bad worker token"),
                        401,
                    );
                }
                Access::Public => {
                    let rb = anon.bare(method.clone(), &url);
                    let (st, body) = if method == Method::POST {
                        anon.send(rb.json(&serde_json::json!({}))).await
                    } else {
                        anon.send(rb).await
                    };
                    seen_public.push(url.clone());
                    // setup and login answer an empty body with 400, never with a missing-session 401.
                    assert!(st == 200 || st == 400, "{m} {url}: {st} {body}");
                }
            }
        }
    }
    assert_eq!(
        seen_public,
        [
            "/healthz",
            "/api/v1/auth/state",
            "/api/v1/auth/setup",
            "/api/v1/auth/login",
            "/api/v1/openapi.json",
            "/api/docs"
        ],
        "the public API surface changed; review it for AUTH-001"
    );
    // Unknown API paths are not served by the SPA fallback; the application shell needs no credentials.
    assert_eq!(anon.send(anon.bare(Method::GET, "/api/v1/nope")).await.0, 404);
    assert_eq!(anon.send(anon.bare(Method::GET, "/")).await.0, 200);
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn openapi_route_is_public_and_matches_the_runtime_generator() {
    let env = boot().await;
    let api = env.api();
    let (status, body) = api.get("/api/v1/openapi.json").await;
    assert_eq!(status, 200);
    assert_eq!(body["openapi"], "3.1.0");
    assert!(body["paths"]["/api/v1/openapi.json"]["get"].is_object());
    assert!(body["paths"]["/api/docs"]["get"].is_object());
    let (status, html) = api.get("/api/docs").await;
    assert_eq!(status, 200);
    assert!(html.as_str().unwrap().contains("/api/v1/openapi.json"));
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn unknown_api_paths_and_methods_return_problem_details() {
    let env = boot().await;
    let api = env.api();
    let (status, body) = api.get("/api/v1/unknown").await;
    assert_eq!(status, 404);
    assert_eq!(body["type"], "https://llm-tools.dev/problems/not-found");
    let request = api.bare(Method::POST, "/api/v1/missions").json(&serde_json::json!({}));
    let response = request.send().await.unwrap();
    assert_eq!(response.status().as_u16(), 405);
    assert_eq!(response.headers()["content-type"], "application/problem+json");
    assert_eq!(response.headers()["allow"], "GET,HEAD");
    let body: serde_json::Value = response.json().await.unwrap();
    assert_eq!(body["type"], "https://llm-tools.dev/problems/method-not-allowed");
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn every_authenticated_route_enforces_its_declared_token_scope() {
    let env = boot().await;
    let api = env.api();
    let token_scopes = [
        ("scope-read", vec!["read".to_string()]),
        ("scope-operate", vec!["operate".to_string()]),
        ("scope-terminal-view", vec!["terminal:view".to_string()]),
        ("scope-terminal-write", vec!["terminal:write".to_string()]),
        ("scope-admin", vec!["admin".to_string()]),
    ];
    for (name, scopes) in &token_scopes {
        let token = format!("secret-{name}");
        env.srv
            .state
            .db
            .token_create_scoped(name, &bridge_coordinator::auth::sha256_hex(&token), None, scopes)
            .unwrap();
    }
    for (path, methods, access, declared_scope) in ROUTES {
        if *access != Access::Authed {
            assert!(!declared_scope.is_empty() || *access == Access::Public, "{path} has no policy");
            continue;
        }
        assert!(!declared_scope.is_empty(), "{path} has no scope policy");
        for method in *methods {
            let method: Method = method.parse().unwrap();
            for (name, granted) in &token_scopes {
                let required = if *declared_scope == "terminal:conditional" {
                    if *name == "scope-terminal-write" {
                        "terminal:write"
                    } else {
                        "terminal:view"
                    }
                } else {
                    declared_scope
                };
                let has_scope = bridge_coordinator::scopes::grants(granted, required);
                let mut url = concrete(path);
                if *path == "/api/v1/terminal" {
                    url.push_str(if required == "terminal:write" {
                        "?worker=w1&mission=m1&mode=write"
                    } else {
                        "?worker=w1&mission=m1"
                    });
                }
                let token = format!("secret-{name}");
                let request = api.bare(method.clone(), &url).header("authorization", format!("Bearer {token}"));
                let request = if method == Method::POST {
                    request.json(&serde_json::json!({"name": "scope-created"}))
                } else {
                    request
                };
                let (status, body) = api.send(request).await;
                if has_scope {
                    assert_ne!(status, 401, "{method} {url} token {name}: {body}");
                    assert_ne!(status, 403, "{method} {url} token {name}: {body}");
                } else {
                    assert_eq!(status, 403, "{method} {url} token {name}: {body}");
                }
            }
        }
    }
    env.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn revoked_worker_token_is_refused_on_both_worker_endpoints() {
    let env = boot().await;
    let api = env.admin().await;
    let token = api.add_worker("w1").await;
    let (st, _) = api.send(api.rb(Method::DELETE, "/api/v1/workers/w1")).await;
    assert_eq!(st, 200);
    for path in ["/api/v1/worker/connect", "/api/v1/worker/terminal/s1"] {
        refused_with(env.ws(path, &[("authorization", &format!("Bearer {token}"))]).await.expect_err("revoked"), 401);
    }
    env.stop().await;
}
