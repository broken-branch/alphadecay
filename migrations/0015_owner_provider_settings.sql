CREATE TABLE owner_provider_settings (
    singleton_id varchar(32) PRIMARY KEY
        CHECK (singleton_id = 'owner-ai-provider'),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    provider varchar(32),
    endpoint varchar(2048),
    model varchar(256),
    generation bigint NOT NULL CHECK (generation > 0),
    credential_nonce bytea,
    credential_ciphertext bytea,
    active boolean NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    CONSTRAINT ck_owner_provider_settings_state CHECK (
        (active = true
         AND provider IN ('OWNER_GEMINI', 'OWNER_OPENAI_COMPATIBLE')
         AND endpoint IS NOT NULL
         AND model IS NOT NULL
         AND credential_nonce IS NOT NULL
         AND octet_length(credential_nonce) = 12
         AND credential_ciphertext IS NOT NULL
         AND octet_length(credential_ciphertext) BETWEEN 17 AND 16400)
        OR
        (active = false
         AND provider IS NULL
         AND endpoint IS NULL
         AND model IS NULL
         AND credential_nonce IS NULL
         AND credential_ciphertext IS NULL)
    )
);

CREATE FUNCTION owner_provider_settings_mutation_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.generation <> 1 OR NEW.updated_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'OWNER_PROVIDER_SETTINGS_MUTATION_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'OWNER_PROVIDER_SETTINGS_DELETE_FORBIDDEN';
    END IF;
    IF NEW.singleton_id IS DISTINCT FROM OLD.singleton_id
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.generation IS DISTINCT FROM OLD.generation + 1
       OR NEW.updated_at < OLD.updated_at
    THEN
        RAISE EXCEPTION 'OWNER_PROVIDER_SETTINGS_MUTATION_INVALID';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER owner_provider_settings_mutation_guard
BEFORE INSERT OR UPDATE OR DELETE ON owner_provider_settings
FOR EACH ROW EXECUTE FUNCTION owner_provider_settings_mutation_guard();
