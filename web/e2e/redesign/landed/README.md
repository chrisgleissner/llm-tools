One file per landed work package (`WP-0` ... `WP-5`, `lead`), created by the commit that lands the package:

    touch web/e2e/redesign/landed/WP-3

`gate.ts` runs every acceptance test whose needed packages all have a file here and registers the others with
`test.fixme`. See `gate.ts` for the environment overrides.
