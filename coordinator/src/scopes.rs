//! API token scope names and the administrator's all-scope grant.

pub const READ: &str = "read";
pub const OPERATE: &str = "operate";
pub const TERMINAL_VIEW: &str = "terminal:view";
pub const TERMINAL_WRITE: &str = "terminal:write";
pub const ADMIN: &str = "admin";
pub const ALL: &[&str] = &[READ, OPERATE, TERMINAL_VIEW, TERMINAL_WRITE, ADMIN];

pub fn valid(scope: &str) -> bool {
    ALL.contains(&scope)
}

pub fn grants(scopes: &[String], required: &str) -> bool {
    scopes.iter().any(|scope| scope == ADMIN || scope == required)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn admin_grants_every_scope_but_operate_does_not_grant_read() {
        assert!(grants(&[ADMIN.to_string()], READ));
        assert!(grants(&[ADMIN.to_string()], TERMINAL_WRITE));
        assert!(grants(&[OPERATE.to_string()], OPERATE));
        assert!(!grants(&[OPERATE.to_string()], READ));
    }
}
