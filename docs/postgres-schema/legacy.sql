--
-- PostgreSQL database dump
--

\restrict yIYlQSmTHx4N2tP1aSlgw5Pn4RWTyYzcmyni2WG9gL9oFZ9zjZrS1Scq1VJtkRu

-- Dumped from database version 17.5 (Ubuntu 17.5-1.pgdg24.04+1)
-- Dumped by pg_dump version 17.7 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: echr_touch_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.echr_touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;


--
-- Name: echr_update_citation_counts(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.echr_update_citation_counts() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_old_source VARCHAR(50);
    v_old_target VARCHAR(50);
    v_new_source VARCHAR(50);
    v_new_target VARCHAR(50);
BEGIN
    -- -----------------------------------------------------------------------
    -- DELETE: decrement counts for the removed edge
    -- -----------------------------------------------------------------------
    IF TG_OP = 'DELETE' THEN
        v_old_source := OLD.source_itemid;
        v_old_target := OLD.target_itemid;

        -- Decrement cites_count for source (source cites target, so source loses a "cites")
        IF v_old_source IS NOT NULL THEN
            UPDATE echr_citation_counts
            SET cites_count = GREATEST(cites_count - 1, 0),
                updated_at = NOW()
            WHERE itemid = v_old_source;
        END IF;

        -- Decrement cited_by_count for target (target was cited by source)
        IF v_old_target IS NOT NULL THEN
            UPDATE echr_citation_counts
            SET cited_by_count = GREATEST(cited_by_count - 1, 0),
                updated_at = NOW()
            WHERE itemid = v_old_target;
        END IF;

        RETURN OLD;
    END IF;

    -- -----------------------------------------------------------------------
    -- INSERT: increment counts for the new edge
    -- -----------------------------------------------------------------------
    IF TG_OP = 'INSERT' THEN
        v_new_source := NEW.source_itemid;
        v_new_target := NEW.target_itemid;

        -- Increment cites_count for source
        IF v_new_source IS NOT NULL THEN
            INSERT INTO echr_citation_counts (itemid, cites_count, cited_by_count, updated_at)
            VALUES (v_new_source, 1, 0, NOW())
            ON CONFLICT (itemid) DO UPDATE
            SET cites_count = echr_citation_counts.cites_count + 1,
                updated_at = NOW();
        END IF;

        -- Increment cited_by_count for target
        IF v_new_target IS NOT NULL THEN
            INSERT INTO echr_citation_counts (itemid, cites_count, cited_by_count, updated_at)
            VALUES (v_new_target, 0, 1, NOW())
            ON CONFLICT (itemid) DO UPDATE
            SET cited_by_count = echr_citation_counts.cited_by_count + 1,
                updated_at = NOW();
        END IF;

        RETURN NEW;
    END IF;

    -- -----------------------------------------------------------------------
    -- UPDATE: handle changes to source_itemid and/or target_itemid
    -- Only adjust counts for columns that actually changed.
    -- -----------------------------------------------------------------------
    IF TG_OP = 'UPDATE' THEN
        v_old_source := OLD.source_itemid;
        v_old_target := OLD.target_itemid;
        v_new_source := NEW.source_itemid;
        v_new_target := NEW.target_itemid;

        -- Handle source_itemid change
        IF (v_old_source IS DISTINCT FROM v_new_source) THEN
            -- Decrement old source's cites_count
            IF v_old_source IS NOT NULL THEN
                UPDATE echr_citation_counts
                SET cites_count = GREATEST(cites_count - 1, 0),
                    updated_at = NOW()
                WHERE itemid = v_old_source;
            END IF;

            -- Increment new source's cites_count
            IF v_new_source IS NOT NULL THEN
                INSERT INTO echr_citation_counts (itemid, cites_count, cited_by_count, updated_at)
                VALUES (v_new_source, 1, 0, NOW())
                ON CONFLICT (itemid) DO UPDATE
                SET cites_count = echr_citation_counts.cites_count + 1,
                    updated_at = NOW();
            END IF;
        END IF;

        -- Handle target_itemid change
        IF (v_old_target IS DISTINCT FROM v_new_target) THEN
            -- Decrement old target's cited_by_count
            IF v_old_target IS NOT NULL THEN
                UPDATE echr_citation_counts
                SET cited_by_count = GREATEST(cited_by_count - 1, 0),
                    updated_at = NOW()
                WHERE itemid = v_old_target;
            END IF;

            -- Increment new target's cited_by_count
            IF v_new_target IS NOT NULL THEN
                INSERT INTO echr_citation_counts (itemid, cites_count, cited_by_count, updated_at)
                VALUES (v_new_target, 0, 1, NOW())
                ON CONFLICT (itemid) DO UPDATE
                SET cited_by_count = echr_citation_counts.cited_by_count + 1,
                    updated_at = NOW();
            END IF;
        END IF;

        RETURN NEW;
    END IF;

    -- Should never reach here
    RETURN NULL;
END;
$$;


--
-- Name: FUNCTION echr_update_citation_counts(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.echr_update_citation_counts() IS 'Trigger function to maintain echr_citation_counts on echr_edge changes. Handles INSERT, UPDATE (including itemid changes), and DELETE. Uses UPSERT for safe concurrent access and GREATEST() to prevent negative counts.';


--
-- Name: rs_date_to_iso(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rs_date_to_iso(d date) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
    SELECT lpad(extract(year from d)::text, 4, '0')
        || '-' || lpad(extract(month from d)::text, 2, '0')
        || '-' || lpad(extract(day from d)::text, 2, '0')
$$;


--
-- Name: rs_document_text_compute_tsv(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rs_document_text_compute_tsv() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    d_summary          text;
    d_legal_provisions text[];
BEGIN
    SELECT summary, legal_provisions
      INTO d_summary, d_legal_provisions
      FROM rs_document
     WHERE ecli = NEW.ecli;

    NEW.fulltext_tsv := to_tsvector(
        'simple',
        COALESCE(d_summary, '') || ' ' ||
        COALESCE(NEW.fulltext, '') || ' ' ||
        COALESCE(array_to_string(d_legal_provisions, ' '), '')
    );
    RETURN NEW;
END;
$$;


--
-- Name: rs_touch_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rs_touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: rs_update_citation_counts(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rs_update_citation_counts() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        UPDATE rs_citation_counts
        SET cites_count = GREATEST(cites_count - 1, 0), updated_at = NOW()
        WHERE ecli = OLD.source_ecli;

        UPDATE rs_citation_counts
        SET cited_by_count = GREATEST(cited_by_count - 1, 0), updated_at = NOW()
        WHERE ecli = OLD.target_ecli;
        RETURN OLD;
    END IF;

    IF TG_OP = 'INSERT' THEN
        INSERT INTO rs_citation_counts (ecli, cites_count, cited_by_count, updated_at)
        VALUES (NEW.source_ecli, 1, 0, NOW())
        ON CONFLICT (ecli) DO UPDATE
        SET cites_count = rs_citation_counts.cites_count + 1,
            updated_at = NOW();

        INSERT INTO rs_citation_counts (ecli, cites_count, cited_by_count, updated_at)
        VALUES (NEW.target_ecli, 0, 1, NOW())
        ON CONFLICT (ecli) DO UPDATE
        SET cited_by_count = rs_citation_counts.cited_by_count + 1,
            updated_at = NOW();
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.source_ecli IS DISTINCT FROM NEW.source_ecli THEN
            UPDATE rs_citation_counts
            SET cites_count = GREATEST(cites_count - 1, 0), updated_at = NOW()
            WHERE ecli = OLD.source_ecli;

            INSERT INTO rs_citation_counts (ecli, cites_count, cited_by_count, updated_at)
            VALUES (NEW.source_ecli, 1, 0, NOW())
            ON CONFLICT (ecli) DO UPDATE
            SET cites_count = rs_citation_counts.cites_count + 1,
                updated_at = NOW();
        END IF;

        IF OLD.target_ecli IS DISTINCT FROM NEW.target_ecli THEN
            UPDATE rs_citation_counts
            SET cited_by_count = GREATEST(cited_by_count - 1, 0), updated_at = NOW()
            WHERE ecli = OLD.target_ecli;

            INSERT INTO rs_citation_counts (ecli, cites_count, cited_by_count, updated_at)
            VALUES (NEW.target_ecli, 0, 1, NOW())
            ON CONFLICT (ecli) DO UPDATE
            SET cited_by_count = rs_citation_counts.cited_by_count + 1,
                updated_at = NOW();
        END IF;
        RETURN NEW;
    END IF;

    RETURN NULL;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: case_law; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.case_law (
    id integer NOT NULL,
    case_id integer,
    law_id integer,
    source text,
    jc_id text,
    lido_id text,
    opschrift text,
    CONSTRAINT case_law_staging_source_check CHECK ((source = ANY (ARRAY['lido-ref'::text, 'lido-linkt'::text, 'custom'::text])))
);


--
-- Name: case_law_staging_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.case_law_staging_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_law_staging_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.case_law_staging_id_seq OWNED BY public.case_law.id;


--
-- Name: echr_citation_counts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_citation_counts (
    itemid character varying(50) NOT NULL,
    cites_count integer DEFAULT 0 NOT NULL,
    cited_by_count integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE echr_citation_counts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.echr_citation_counts IS 'Pre-computed citation counts per ECHR case (itemid). Maintained automatically by triggers on echr_edge.';


--
-- Name: COLUMN echr_citation_counts.cites_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_citation_counts.cites_count IS 'Number of outgoing citations (edges where this itemid is source)';


--
-- Name: COLUMN echr_citation_counts.cited_by_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_citation_counts.cited_by_count IS 'Number of incoming citations (edges where this itemid is target)';


--
-- Name: echr_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_document (
    itemid character varying(50) NOT NULL,
    languageisocode character varying(10) NOT NULL,
    ecli character varying(100),
    appno text,
    extractedappno text,
    docname text,
    doctype character varying(50),
    doctypebranch character varying(50),
    judgementdate timestamp with time zone,
    referencedate timestamp with time zone,
    article text,
    conclusion text,
    violation text,
    nonviolation text,
    respondent character varying(500),
    originatingbody integer,
    representedby text,
    publishedby text,
    rulesofcourt character varying(50),
    applicability text,
    separateopinion text,
    issue text,
    importance smallint,
    rank numeric,
    scl text,
    externalsources text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    judgement_year integer GENERATED ALWAYS AS ((EXTRACT(year FROM (judgementdate AT TIME ZONE 'UTC'::text)))::integer) STORED
);


--
-- Name: TABLE echr_document; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.echr_document IS 'ECHR HUDOC metadata (one row per itemid + language)';


--
-- Name: COLUMN echr_document.itemid; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.itemid IS 'HUDOC item identifier (business key)';


--
-- Name: COLUMN echr_document.languageisocode; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.languageisocode IS 'Language code (ENG/FRE/...)';


--
-- Name: COLUMN echr_document.ecli; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.ecli IS 'European Case Law Identifier';


--
-- Name: COLUMN echr_document.appno; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.appno IS 'Application number(s), semicolon-separated';


--
-- Name: COLUMN echr_document.extractedappno; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.extractedappno IS 'Application numbers parsed from references';


--
-- Name: COLUMN echr_document.scl; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.scl IS 'Case references/citations (raw)';


--
-- Name: COLUMN echr_document.externalsources; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.externalsources IS 'External sources (raw)';


--
-- Name: COLUMN echr_document.judgement_year; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document.judgement_year IS 'Generated year (UTC) extracted from judgementdate';


--
-- Name: echr_document_appno; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_document_appno (
    itemid character varying(50) NOT NULL,
    languageisocode character varying(10) NOT NULL,
    appno text NOT NULL,
    source character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE echr_document_appno; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.echr_document_appno IS 'Normalized application numbers - one row per appno for fast lookups';


--
-- Name: COLUMN echr_document_appno.appno; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document_appno.appno IS 'Individual application number (split from semicolon/comma-separated values)';


--
-- Name: COLUMN echr_document_appno.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document_appno.source IS 'Source field: "appno" (case own appno) or "extractedappno" (from references)';


--
-- Name: echr_document_article; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_document_article (
    itemid character varying(50) NOT NULL,
    languageisocode character varying(10) NOT NULL,
    kind character varying(20) NOT NULL,
    article_code text NOT NULL,
    CONSTRAINT echr_document_article_kind_check CHECK (((kind)::text = ANY ((ARRAY['applied'::character varying, 'violation'::character varying, 'nonviolation'::character varying])::text[])))
);


--
-- Name: echr_document_text; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_document_text (
    itemid character varying(50) NOT NULL,
    languageisocode character varying(10) NOT NULL,
    fulltext text,
    fulltext_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(fulltext, ''::text))) STORED
);


--
-- Name: TABLE echr_document_text; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.echr_document_text IS 'Full text of ECHR documents per itemid + language';


--
-- Name: COLUMN echr_document_text.fulltext_tsv; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_document_text.fulltext_tsv IS 'tsvector for full-text search (index later)';


--
-- Name: echr_edge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_edge (
    id bigint NOT NULL,
    source_itemid character varying(50) NOT NULL,
    target_itemid character varying(50) NOT NULL,
    source_ecli character varying(100),
    target_ecli character varying(100),
    weight integer DEFAULT 1,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE echr_edge; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.echr_edge IS 'Citation edges between ECHR cases (itemid→itemid)';


--
-- Name: COLUMN echr_edge.weight; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.echr_edge.weight IS 'Number of citations (>=1)';


--
-- Name: echr_edge_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.echr_edge_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: echr_edge_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.echr_edge_id_seq OWNED BY public.echr_edge.id;


--
-- Name: echr_extractor_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.echr_extractor_segments (
    itemid character varying(50) NOT NULL,
    languageisocode character varying(10) NOT NULL,
    ecli character varying(100),
    parser_mode character varying(50),
    error text,
    procedure text,
    facts text,
    complaints text,
    law text,
    operative text,
    subject_matter text,
    court_assessment text,
    separate_opinion text,
    appendix text,
    num_sections integer DEFAULT 0 NOT NULL,
    segmented_at timestamp with time zone DEFAULT now() NOT NULL,
    extractor_version text
);


--
-- Name: echr_v_document_with_text; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.echr_v_document_with_text AS
 SELECT d.itemid,
    d.languageisocode,
    d.ecli,
    d.appno,
    d.extractedappno,
    d.docname,
    d.doctype,
    d.doctypebranch,
    d.judgementdate,
    d.referencedate,
    d.article,
    d.conclusion,
    d.violation,
    d.nonviolation,
    d.respondent,
    d.originatingbody,
    d.representedby,
    d.publishedby,
    d.rulesofcourt,
    d.applicability,
    d.separateopinion,
    d.issue,
    d.importance,
    d.rank,
    d.scl,
    d.externalsources,
    d.created_at,
    d.updated_at,
    d.judgement_year,
    t.fulltext,
    t.fulltext_tsv
   FROM (public.echr_document d
     LEFT JOIN public.echr_document_text t ON ((((t.itemid)::text = (d.itemid)::text) AND ((t.languageisocode)::text = (d.languageisocode)::text))));


--
-- Name: echr_v_edge_full; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.echr_v_edge_full AS
 SELECT e.id,
    e.source_itemid,
    e.target_itemid,
    e.source_ecli,
    e.target_ecli,
    e.weight,
    e.created_at,
    ds.docname AS source_docname,
    ds.doctype AS source_doctype,
    ds.judgementdate AS source_judgementdate,
    ds.ecli AS source_ecli_full,
    ds.languageisocode AS source_language,
    dt.docname AS target_docname,
    dt.doctype AS target_doctype,
    dt.judgementdate AS target_judgementdate,
    dt.ecli AS target_ecli_full,
    dt.languageisocode AS target_language
   FROM ((public.echr_edge e
     LEFT JOIN public.echr_document ds ON (((ds.itemid)::text = (e.source_itemid)::text)))
     LEFT JOIN public.echr_document dt ON (((dt.itemid)::text = (e.target_itemid)::text)));


--
-- Name: echr_v_judgments_decisions; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.echr_v_judgments_decisions AS
 SELECT itemid,
    languageisocode,
    ecli,
    appno,
    extractedappno,
    docname,
    doctype,
    doctypebranch,
    judgementdate,
    referencedate,
    article,
    conclusion,
    violation,
    nonviolation,
    respondent,
    originatingbody,
    representedby,
    publishedby,
    rulesofcourt,
    applicability,
    separateopinion,
    issue,
    importance,
    rank,
    scl,
    externalsources,
    created_at,
    updated_at,
    judgement_year
   FROM public.echr_document d
  WHERE (((doctype)::text ~~* '%JUD%'::text) OR ((doctype)::text ~~* '%DEC%'::text));


--
-- Name: ecli_bwb_opschrift; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecli_bwb_opschrift (
    ecli text NOT NULL,
    opschrift text
);


--
-- Name: ecli_keywords; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecli_keywords (
    id integer NOT NULL,
    ecli character varying(50),
    keyword character varying(255) NOT NULL,
    method character varying(50) NOT NULL,
    score double precision,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: ecli_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecli_segments (
    id integer NOT NULL,
    ecli character varying(50),
    segment text NOT NULL,
    segment_hash character varying(64) NOT NULL,
    embedding public.vector(768)
);


--
-- Name: ecli_segments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecli_segments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ecli_segments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecli_segments_id_seq OWNED BY public.ecli_segments.id;


--
-- Name: ecli_summary_keywords_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecli_summary_keywords_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ecli_summary_keywords_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecli_summary_keywords_id_seq OWNED BY public.ecli_keywords.id;


--
-- Name: ecli_texts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecli_texts (
    ecli character varying(50) NOT NULL,
    full_text text NOT NULL,
    summary text,
    link text
);


--
-- Name: law_alias; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.law_alias (
    id integer NOT NULL,
    alias text NOT NULL,
    bwb_id text NOT NULL,
    source text,
    CONSTRAINT law_alias_source_check CHECK ((source = ANY (ARRAY['opschrift'::text, 'bwbidlist'::text])))
);


--
-- Name: law_alias_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.law_alias_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: law_alias_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.law_alias_id_seq OWNED BY public.law_alias.id;


--
-- Name: law_element; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.law_element (
    id integer NOT NULL,
    type text,
    bwb_id text,
    bwb_label_id bigint,
    lido_id text,
    jc_id text,
    number text,
    title text,
    CONSTRAINT law_element_staging_type_check CHECK ((type = ANY (ARRAY['wet'::text, 'boek'::text, 'deel'::text, 'titeldeel'::text, 'hoofdstuk'::text, 'artikel'::text, 'paragraaf'::text, 'subparagraaf'::text, 'afdeling'::text])))
);


--
-- Name: law_element_staging_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.law_element_staging_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: law_element_staging_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.law_element_staging_id_seq OWNED BY public.law_element.id;


--
-- Name: legal_case; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.legal_case (
    id integer NOT NULL,
    ecli_id text NOT NULL,
    title text,
    celex_id text,
    zaaknummer text,
    uitspraakdatum date
);


--
-- Name: legal_case_staging_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.legal_case_staging_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: legal_case_staging_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.legal_case_staging_id_seq OWNED BY public.legal_case.id;


--
-- Name: rs_citation_counts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_citation_counts (
    ecli text NOT NULL,
    cites_count integer DEFAULT 0 NOT NULL,
    cited_by_count integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: rs_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_document (
    ecli text NOT NULL,
    date_decision date,
    document_type text,
    instance text,
    domains text[],
    source text DEFAULT 'Rechtspraak'::text,
    jurisdiction_country text DEFAULT 'NL'::text,
    procedure_type text,
    url_publication text,
    summary text,
    legal_provisions text[],
    predecessor_successor_cases text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    date_published date,
    date_issued date,
    date_modified timestamp with time zone,
    title text,
    language text,
    access_rights text,
    zittingsplaats text,
    replaces_identifier text,
    creator_uri text,
    vindplaatsen text[],
    subject_uris text[],
    zaaknummer text,
    opendata_status text DEFAULT 'public'::text NOT NULL,
    CONSTRAINT rs_document_opendata_status_check CHECK ((opendata_status = ANY (ARRAY['public'::text, 'depublicated'::text])))
);


--
-- Name: rs_document_external_authority; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_document_external_authority (
    ecli text NOT NULL,
    kind text DEFAULT 'other'::text NOT NULL,
    name text NOT NULL,
    article text,
    raw text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: rs_document_formal_relation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_document_formal_relation (
    ecli text NOT NULL,
    target_ecli text,
    target_identifier text NOT NULL,
    relation_type text DEFAULT 'unknown'::text NOT NULL,
    aanleg text DEFAULT 'unknown'::text NOT NULL,
    name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    disposition text,
    gevolg text
);


--
-- Name: COLUMN rs_document_formal_relation.gevolg; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.rs_document_formal_relation.gevolg IS 'psi:gevolg outcome attribute on dcterms:relation. v12 (sql/023). Examples: vernietiging en zelf afgedaan, gevolgd, bekrachtiging/bevestiging, niet ontvankelijk.';


--
-- Name: rs_document_law_reference; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_document_law_reference (
    ecli text NOT NULL,
    bwb_resource text NOT NULL,
    article text DEFAULT ''::text NOT NULL,
    version_date date,
    bwb_label_id bigint,
    source text NOT NULL,
    opschrift text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    legal_provision_url text GENERATED ALWAYS AS ((((('http://wetten.overheid.nl/id/'::text || bwb_resource) || '/'::text) || COALESCE(public.rs_date_to_iso(version_date), '1900-01-01'::text)) || '/0'::text)) STORED,
    legal_provision_url_lido text GENERATED ALWAYS AS (
CASE
    WHEN (bwb_label_id IS NULL) THEN NULL::text
    ELSE ((((((('http://linkeddata.overheid.nl/terms/bwb/id/'::text || bwb_resource) || '/'::text) || (bwb_label_id)::text) || '/'::text) || COALESCE(public.rs_date_to_iso(version_date), '1900-01-01'::text)) || '/'::text) || COALESCE(public.rs_date_to_iso(version_date), '1900-01-01'::text))
END) STORED
);


--
-- Name: COLUMN rs_document_law_reference.legal_provision_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.rs_document_law_reference.legal_provision_url IS 'wetten.overheid.nl deeplink. GENERATED ALWAYS from (bwb_resource, version_date). v12 sql/024.';


--
-- Name: COLUMN rs_document_law_reference.legal_provision_url_lido; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.rs_document_law_reference.legal_provision_url_lido IS 'LIDO deeplink. GENERATED ALWAYS from (bwb_resource, bwb_label_id, version_date). NULL when bwb_label_id is not yet resolved; recomputes automatically when resolve-bwb fills it in. v12 sql/024.';


--
-- Name: rs_document_publication; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_document_publication (
    ecli text NOT NULL,
    raw text NOT NULL,
    kind text DEFAULT 'other'::text NOT NULL,
    journal_abbr text,
    year integer,
    locator text,
    annotator text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: rs_document_text; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_document_text (
    ecli text NOT NULL,
    fulltext text,
    fulltext_tsv tsvector,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: rs_edge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_edge (
    source_ecli text NOT NULL,
    target_ecli text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    relation_type text,
    source text DEFAULT 'body-cite'::text NOT NULL
);


--
-- Name: rs_law_alias; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_law_alias (
    id bigint NOT NULL,
    alias text NOT NULL,
    bwb_id text NOT NULL,
    snapshot_date date DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: rs_law_element; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rs_law_element (
    id bigint NOT NULL,
    type text,
    bwb_id text,
    bwb_label_id bigint,
    lido_id text,
    jc_id text,
    number text,
    title text,
    snapshot_date date DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: rs_v_document_legal_provisions; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.rs_v_document_legal_provisions AS
 SELECT DISTINCT lr.ecli,
    lr.opschrift AS legal_provision
   FROM public.rs_document_law_reference lr
  WHERE (NULLIF(lr.opschrift, ''::text) IS NOT NULL)
UNION
 SELECT DISTINCT lr.ecli,
    le.title AS legal_provision
   FROM (public.rs_document_law_reference lr
     JOIN public.rs_law_element le ON ((le.bwb_label_id = lr.bwb_label_id)))
  WHERE ((lr.bwb_label_id IS NOT NULL) AND (NULLIF(le.title, ''::text) IS NOT NULL))
UNION
 SELECT DISTINCT lr.ecli,
    le.title AS legal_provision
   FROM (public.rs_document_law_reference lr
     JOIN public.rs_law_element le ON (((le.bwb_id = lr.bwb_resource) AND (lower(le.number) = lower(lr.article)) AND (le.type = 'artikel'::text))))
  WHERE (NULLIF(le.title, ''::text) IS NOT NULL)
UNION
 SELECT DISTINCT lr.ecli,
    wet.title AS legal_provision
   FROM (public.rs_document_law_reference lr
     JOIN public.rs_law_element wet ON (((wet.bwb_id = lr.bwb_resource) AND (wet.type = 'wet'::text))))
  WHERE (NULLIF(wet.title, ''::text) IS NOT NULL)
UNION
 SELECT DISTINCT lr.ecli,
    ((wet.title || ', Artikel '::text) || lr.article) AS legal_provision
   FROM (public.rs_document_law_reference lr
     JOIN public.rs_law_element wet ON (((wet.bwb_id = lr.bwb_resource) AND (wet.type = 'wet'::text))))
  WHERE ((NULLIF(wet.title, ''::text) IS NOT NULL) AND (NULLIF(lr.article, ''::text) IS NOT NULL))
UNION
 SELECT DISTINCT lr.ecli,
    ((wet.title || ', Bijlage '::text) || lr.article) AS legal_provision
   FROM (public.rs_document_law_reference lr
     JOIN public.rs_law_element wet ON (((wet.bwb_id = lr.bwb_resource) AND (wet.type = 'wet'::text))))
  WHERE ((NULLIF(wet.title, ''::text) IS NOT NULL) AND (NULLIF(lr.article, ''::text) IS NOT NULL) AND (lr.opschrift ~~* '%bijlage%'::text));


--
-- Name: VIEW rs_v_document_legal_provisions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.rs_v_document_legal_provisions IS 'Legal-provision display labels for /api/rechtspraak from rs_* tables only: stored rs_document_law_reference.opschrift plus canonical rs_law_element.title where resolvable.';


--
-- Name: rs_v_document_with_text; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.rs_v_document_with_text AS
 SELECT d.ecli,
    d.date_decision,
    d.document_type,
    d.instance,
    d.domains,
    d.source,
    d.jurisdiction_country,
    d.procedure_type,
    d.url_publication,
    d.summary,
    d.legal_provisions,
    d.predecessor_successor_cases,
    d.created_at,
    d.updated_at,
    d.date_published,
    d.date_issued,
    d.date_modified,
    d.title,
    d.language,
    d.access_rights,
    d.zittingsplaats,
    d.replaces_identifier,
    d.creator_uri,
    d.vindplaatsen,
    d.subject_uris,
    d.zaaknummer,
    d.opendata_status,
    t.fulltext,
    t.fulltext_tsv
   FROM (public.rs_document d
     LEFT JOIN public.rs_document_text t ON ((t.ecli = d.ecli)));


--
-- Name: case_law id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.case_law ALTER COLUMN id SET DEFAULT nextval('public.case_law_staging_id_seq'::regclass);


--
-- Name: echr_edge id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_edge ALTER COLUMN id SET DEFAULT nextval('public.echr_edge_id_seq'::regclass);


--
-- Name: ecli_keywords id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_keywords ALTER COLUMN id SET DEFAULT nextval('public.ecli_summary_keywords_id_seq'::regclass);


--
-- Name: ecli_segments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_segments ALTER COLUMN id SET DEFAULT nextval('public.ecli_segments_id_seq'::regclass);


--
-- Name: law_alias id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.law_alias ALTER COLUMN id SET DEFAULT nextval('public.law_alias_id_seq'::regclass);


--
-- Name: law_element id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.law_element ALTER COLUMN id SET DEFAULT nextval('public.law_element_staging_id_seq'::regclass);


--
-- Name: legal_case id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_case ALTER COLUMN id SET DEFAULT nextval('public.legal_case_staging_id_seq'::regclass);


--
-- Name: case_law case_law_staging_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.case_law
    ADD CONSTRAINT case_law_staging_pkey PRIMARY KEY (id);


--
-- Name: echr_citation_counts echr_citation_counts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_citation_counts
    ADD CONSTRAINT echr_citation_counts_pkey PRIMARY KEY (itemid);


--
-- Name: echr_document_appno echr_document_appno_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document_appno
    ADD CONSTRAINT echr_document_appno_pkey PRIMARY KEY (itemid, languageisocode, appno, source);


--
-- Name: echr_document_article echr_document_article_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document_article
    ADD CONSTRAINT echr_document_article_pkey PRIMARY KEY (itemid, languageisocode, kind, article_code);


--
-- Name: echr_document echr_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document
    ADD CONSTRAINT echr_document_pkey PRIMARY KEY (itemid, languageisocode);


--
-- Name: echr_document_text echr_document_text_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document_text
    ADD CONSTRAINT echr_document_text_pkey PRIMARY KEY (itemid, languageisocode);


--
-- Name: echr_edge echr_edge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_edge
    ADD CONSTRAINT echr_edge_pkey PRIMARY KEY (id);


--
-- Name: echr_extractor_segments echr_extractor_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_extractor_segments
    ADD CONSTRAINT echr_extractor_segments_pkey PRIMARY KEY (itemid, languageisocode);


--
-- Name: ecli_bwb_opschrift ecli_bwb_opschrift_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_bwb_opschrift
    ADD CONSTRAINT ecli_bwb_opschrift_pkey PRIMARY KEY (ecli);


--
-- Name: ecli_segments ecli_segments_ecli_segment_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_segments
    ADD CONSTRAINT ecli_segments_ecli_segment_hash_key UNIQUE (ecli, segment_hash);


--
-- Name: ecli_segments ecli_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_segments
    ADD CONSTRAINT ecli_segments_pkey PRIMARY KEY (id);


--
-- Name: ecli_keywords ecli_summary_keywords_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_keywords
    ADD CONSTRAINT ecli_summary_keywords_pkey PRIMARY KEY (id);


--
-- Name: ecli_texts ecli_texts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_texts
    ADD CONSTRAINT ecli_texts_pkey PRIMARY KEY (ecli);


--
-- Name: law_alias law_alias_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.law_alias
    ADD CONSTRAINT law_alias_pkey PRIMARY KEY (id);


--
-- Name: law_element law_element_jc_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.law_element
    ADD CONSTRAINT law_element_jc_id_key UNIQUE (jc_id);


--
-- Name: law_element law_element_lido_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.law_element
    ADD CONSTRAINT law_element_lido_id_key UNIQUE (lido_id);


--
-- Name: law_element law_element_staging_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.law_element
    ADD CONSTRAINT law_element_staging_pkey PRIMARY KEY (id);


--
-- Name: legal_case legal_case_celex_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_case
    ADD CONSTRAINT legal_case_celex_id_key UNIQUE (celex_id);


--
-- Name: legal_case legal_case_ecli_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_case
    ADD CONSTRAINT legal_case_ecli_id_key UNIQUE (ecli_id);


--
-- Name: legal_case legal_case_staging_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_case
    ADD CONSTRAINT legal_case_staging_pkey PRIMARY KEY (id);


--
-- Name: rs_citation_counts rs_citation_counts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_citation_counts
    ADD CONSTRAINT rs_citation_counts_pkey PRIMARY KEY (ecli);


--
-- Name: rs_document_external_authority rs_document_external_authority_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_external_authority
    ADD CONSTRAINT rs_document_external_authority_pkey PRIMARY KEY (ecli, raw);


--
-- Name: rs_document_formal_relation rs_document_formal_relation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_formal_relation
    ADD CONSTRAINT rs_document_formal_relation_pkey PRIMARY KEY (ecli, target_identifier, relation_type, aanleg);


--
-- Name: rs_document_law_reference rs_document_law_reference_pkey1; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_law_reference
    ADD CONSTRAINT rs_document_law_reference_pkey1 PRIMARY KEY (ecli, bwb_resource, article, source);


--
-- Name: rs_document rs_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document
    ADD CONSTRAINT rs_document_pkey PRIMARY KEY (ecli);


--
-- Name: rs_document_publication rs_document_publication_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_publication
    ADD CONSTRAINT rs_document_publication_pkey PRIMARY KEY (ecli, raw);


--
-- Name: rs_document_text rs_document_text_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_text
    ADD CONSTRAINT rs_document_text_pkey PRIMARY KEY (ecli);


--
-- Name: rs_edge rs_edge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_edge
    ADD CONSTRAINT rs_edge_pkey PRIMARY KEY (source_ecli, target_ecli);


--
-- Name: rs_law_alias rs_law_alias_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_law_alias
    ADD CONSTRAINT rs_law_alias_pkey PRIMARY KEY (id);


--
-- Name: rs_law_element rs_law_element_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_law_element
    ADD CONSTRAINT rs_law_element_pkey PRIMARY KEY (id);


--
-- Name: echr_edge uq_echr_edge_src_tgt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_edge
    ADD CONSTRAINT uq_echr_edge_src_tgt UNIQUE (source_itemid, target_itemid);


--
-- Name: idx_case_law_cl; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_case_law_cl ON public.case_law USING btree (case_id, law_id);


--
-- Name: idx_case_law_lc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_case_law_lc ON public.case_law USING btree (law_id, case_id);


--
-- Name: idx_document_article_filter; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_article_filter ON public.echr_document_article USING btree (kind, article_code);


--
-- Name: idx_document_article_itemid_lang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_document_article_itemid_lang ON public.echr_document_article USING btree (itemid, languageisocode);


--
-- Name: idx_echr_citation_counts_itemid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_citation_counts_itemid ON public.echr_citation_counts USING btree (itemid);


--
-- Name: idx_echr_document_appno_appno; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_appno_appno ON public.echr_document_appno USING btree ("left"(appno, 500));


--
-- Name: idx_echr_document_appno_itemid_lang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_appno_itemid_lang ON public.echr_document_appno USING btree (itemid, languageisocode);


--
-- Name: idx_echr_document_appno_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_appno_source ON public.echr_document_appno USING btree (source);


--
-- Name: idx_echr_document_docname_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_docname_trgm ON public.echr_document USING gin (docname public.gin_trgm_ops);


--
-- Name: idx_echr_document_doctype; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_doctype ON public.echr_document USING btree (doctype);


--
-- Name: idx_echr_document_doctypebranch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_doctypebranch ON public.echr_document USING btree (doctypebranch);


--
-- Name: idx_echr_document_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_ecli ON public.echr_document USING btree (ecli);


--
-- Name: idx_echr_document_issue_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_issue_trgm ON public.echr_document USING gin (issue public.gin_trgm_ops);


--
-- Name: idx_echr_document_itemid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_itemid ON public.echr_document USING btree (itemid);


--
-- Name: idx_echr_document_judgement_year; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_judgement_year ON public.echr_document USING btree (judgement_year);


--
-- Name: idx_echr_document_judgementdate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_judgementdate ON public.echr_document USING btree (judgementdate);


--
-- Name: idx_echr_document_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_language ON public.echr_document USING btree (languageisocode);


--
-- Name: idx_echr_document_originatingbody; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_originatingbody ON public.echr_document USING btree (originatingbody);


--
-- Name: idx_echr_document_text_fulltext_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_document_text_fulltext_tsv ON public.echr_document_text USING gin (fulltext_tsv);


--
-- Name: idx_echr_edge_source_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_source_ecli ON public.echr_edge USING btree (source_ecli) WHERE (source_ecli IS NOT NULL);


--
-- Name: idx_echr_edge_source_itemid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_source_itemid ON public.echr_edge USING btree (source_itemid);


--
-- Name: idx_echr_edge_source_target_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_source_target_ecli ON public.echr_edge USING btree (source_ecli, target_ecli) WHERE ((source_ecli IS NOT NULL) AND (target_ecli IS NOT NULL));


--
-- Name: idx_echr_edge_source_target_itemid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_source_target_itemid ON public.echr_edge USING btree (source_itemid, target_itemid);


--
-- Name: idx_echr_edge_target_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_target_ecli ON public.echr_edge USING btree (target_ecli) WHERE (target_ecli IS NOT NULL);


--
-- Name: idx_echr_edge_target_itemid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_target_itemid ON public.echr_edge USING btree (target_itemid);


--
-- Name: idx_echr_edge_weight; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_edge_weight ON public.echr_edge USING btree (weight) WHERE (weight > 1);


--
-- Name: idx_echr_extractor_segments_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_extractor_segments_ecli ON public.echr_extractor_segments USING btree (ecli);


--
-- Name: idx_echr_extractor_segments_num_sections; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_extractor_segments_num_sections ON public.echr_extractor_segments USING btree (num_sections);


--
-- Name: idx_echr_extractor_segments_parser_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_echr_extractor_segments_parser_mode ON public.echr_extractor_segments USING btree (parser_mode);


--
-- Name: idx_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecli ON public.ecli_texts USING btree (ecli) INCLUDE (ecli) WITH (deduplicate_items='true');


--
-- Name: idx_ecli_keywords_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecli_keywords_ecli ON public.ecli_keywords USING btree (ecli);


--
-- Name: idx_ecli_keywords_method; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecli_keywords_method ON public.ecli_keywords USING btree (method);


--
-- Name: idx_ecli_segments_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecli_segments_ecli ON public.ecli_segments USING btree (ecli);


--
-- Name: idx_ecli_segments_embedding_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecli_segments_embedding_cosine ON public.ecli_segments USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_law_alias_index; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_law_alias_index ON public.law_alias USING btree (lower(alias));


--
-- Name: idx_law_alias_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_law_alias_uniq ON public.law_alias USING btree (bwb_id, lower(alias));


--
-- Name: idx_law_element_bwb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_law_element_bwb_id ON public.law_element USING btree (bwb_id, bwb_label_id);


--
-- Name: idx_law_element_bwb_type_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_law_element_bwb_type_number ON public.law_element USING btree (bwb_id, type, number);


--
-- Name: idx_law_element_filter; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_law_element_filter ON public.law_element USING btree (bwb_id, lower(number), type);


--
-- Name: idx_legal_case_ecli_year; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_legal_case_ecli_year ON public.legal_case USING btree (split_part(ecli_id, ':'::text, 4));


--
-- Name: idx_legal_case_id_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_legal_case_id_ecli ON public.legal_case USING btree (id, ecli_id);


--
-- Name: idx_rs_citation_counts_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_citation_counts_ecli ON public.rs_citation_counts USING btree (ecli);


--
-- Name: idx_rs_document_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_date ON public.rs_document USING btree (date_decision);


--
-- Name: idx_rs_document_date_issued; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_date_issued ON public.rs_document USING btree (date_issued);


--
-- Name: idx_rs_document_date_modified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_date_modified ON public.rs_document USING btree (date_modified);


--
-- Name: idx_rs_document_date_published; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_date_published ON public.rs_document USING btree (date_published);


--
-- Name: idx_rs_document_document_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_document_type ON public.rs_document USING btree (document_type);


--
-- Name: idx_rs_document_domains; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_domains ON public.rs_document USING gin (domains);


--
-- Name: idx_rs_document_external_authority_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_external_authority_ecli ON public.rs_document_external_authority USING btree (ecli);


--
-- Name: idx_rs_document_external_authority_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_external_authority_kind ON public.rs_document_external_authority USING btree (kind);


--
-- Name: idx_rs_document_external_authority_name_lower; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_external_authority_name_lower ON public.rs_document_external_authority USING btree (lower(name));


--
-- Name: idx_rs_document_formal_relation_aanleg; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_formal_relation_aanleg ON public.rs_document_formal_relation USING btree (aanleg) WHERE (aanleg IS NOT NULL);


--
-- Name: idx_rs_document_formal_relation_disposition; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_formal_relation_disposition ON public.rs_document_formal_relation USING btree (disposition) WHERE (disposition IS NOT NULL);


--
-- Name: idx_rs_document_formal_relation_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_formal_relation_ecli ON public.rs_document_formal_relation USING btree (ecli);


--
-- Name: idx_rs_document_formal_relation_relation_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_formal_relation_relation_type ON public.rs_document_formal_relation USING btree (relation_type) WHERE (relation_type IS NOT NULL);


--
-- Name: idx_rs_document_formal_relation_target_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_formal_relation_target_ecli ON public.rs_document_formal_relation USING btree (target_ecli) WHERE (target_ecli IS NOT NULL);


--
-- Name: idx_rs_document_instance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_instance ON public.rs_document USING btree (instance);


--
-- Name: idx_rs_document_jurisdiction_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_jurisdiction_country ON public.rs_document USING btree (jurisdiction_country);


--
-- Name: idx_rs_document_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_language ON public.rs_document USING btree (language);


--
-- Name: idx_rs_document_law_reference_article; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_law_reference_article ON public.rs_document_law_reference USING btree (article);


--
-- Name: idx_rs_document_law_reference_bwb_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_law_reference_bwb_resource ON public.rs_document_law_reference USING btree (bwb_resource);


--
-- Name: idx_rs_document_law_reference_ecli; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_law_reference_ecli ON public.rs_document_law_reference USING btree (ecli);


--
-- Name: idx_rs_document_law_reference_label_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_law_reference_label_id ON public.rs_document_law_reference USING btree (bwb_label_id) WHERE (bwb_label_id IS NOT NULL);


--
-- Name: idx_rs_document_law_reference_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_law_reference_source ON public.rs_document_law_reference USING btree (source);


--
-- Name: idx_rs_document_law_reference_version_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_law_reference_version_date ON public.rs_document_law_reference USING btree (version_date);


--
-- Name: idx_rs_document_legal_provisions; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_legal_provisions ON public.rs_document USING gin (legal_provisions);


--
-- Name: idx_rs_document_publication_annotator_lower; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_publication_annotator_lower ON public.rs_document_publication USING btree (lower(annotator)) WHERE (annotator IS NOT NULL);


--
-- Name: idx_rs_document_publication_journal_abbr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_publication_journal_abbr ON public.rs_document_publication USING btree (journal_abbr) WHERE (journal_abbr IS NOT NULL);


--
-- Name: idx_rs_document_publication_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_publication_kind ON public.rs_document_publication USING btree (kind);


--
-- Name: idx_rs_document_publication_year; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_publication_year ON public.rs_document_publication USING btree (year) WHERE (year IS NOT NULL);


--
-- Name: idx_rs_document_replaces_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_replaces_id ON public.rs_document USING btree (replaces_identifier);


--
-- Name: idx_rs_document_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_source ON public.rs_document USING btree (source);


--
-- Name: idx_rs_document_subject_uris; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_subject_uris ON public.rs_document USING gin (subject_uris);


--
-- Name: idx_rs_document_text_fulltext_tsv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_text_fulltext_tsv ON public.rs_document_text USING gin (fulltext_tsv);


--
-- Name: idx_rs_document_vindplaatsen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_vindplaatsen ON public.rs_document USING gin (vindplaatsen);


--
-- Name: idx_rs_document_zaaknummer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_zaaknummer ON public.rs_document USING btree (zaaknummer);


--
-- Name: idx_rs_document_zittingsplaats; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_document_zittingsplaats ON public.rs_document USING btree (zittingsplaats);


--
-- Name: idx_rs_edge_relation_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_edge_relation_type ON public.rs_edge USING btree (relation_type);


--
-- Name: idx_rs_edge_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_edge_source ON public.rs_edge USING btree (source_ecli);


--
-- Name: idx_rs_edge_src_tag; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_edge_src_tag ON public.rs_edge USING btree (source);


--
-- Name: idx_rs_edge_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_edge_target ON public.rs_edge USING btree (target_ecli);


--
-- Name: idx_rs_law_alias_bwb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_law_alias_bwb ON public.rs_law_alias USING btree (bwb_id);


--
-- Name: idx_rs_law_alias_lower; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_law_alias_lower ON public.rs_law_alias USING btree (lower(alias));


--
-- Name: idx_rs_law_element_bwb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_law_element_bwb ON public.rs_law_element USING btree (bwb_id);


--
-- Name: idx_rs_law_element_bwb_num; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_law_element_bwb_num ON public.rs_law_element USING btree (bwb_id, lower(number), type);


--
-- Name: idx_rs_law_element_label; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rs_law_element_label ON public.rs_law_element USING btree (bwb_label_id) WHERE (bwb_label_id IS NOT NULL);


--
-- Name: rs_document_text rs_document_text_tsv_trg; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER rs_document_text_tsv_trg BEFORE INSERT OR UPDATE OF fulltext ON public.rs_document_text FOR EACH ROW EXECUTE FUNCTION public.rs_document_text_compute_tsv();


--
-- Name: echr_document trg_echr_document_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_echr_document_touch BEFORE UPDATE ON public.echr_document FOR EACH ROW EXECUTE FUNCTION public.echr_touch_updated_at();


--
-- Name: echr_edge trg_echr_edge_citation_counts; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_echr_edge_citation_counts AFTER INSERT OR DELETE OR UPDATE ON public.echr_edge FOR EACH ROW EXECUTE FUNCTION public.echr_update_citation_counts();


--
-- Name: TRIGGER trg_echr_edge_citation_counts ON echr_edge; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TRIGGER trg_echr_edge_citation_counts ON public.echr_edge IS 'Maintains echr_citation_counts table on every edge change.';


--
-- Name: rs_document_text trg_rs_document_text_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_rs_document_text_touch BEFORE UPDATE ON public.rs_document_text FOR EACH ROW EXECUTE FUNCTION public.rs_touch_updated_at();


--
-- Name: rs_document trg_rs_document_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_rs_document_touch BEFORE UPDATE ON public.rs_document FOR EACH ROW EXECUTE FUNCTION public.rs_touch_updated_at();


--
-- Name: rs_edge trg_rs_edge_citation_counts; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_rs_edge_citation_counts AFTER INSERT OR DELETE OR UPDATE ON public.rs_edge FOR EACH ROW EXECUTE FUNCTION public.rs_update_citation_counts();

ALTER TABLE public.rs_edge DISABLE TRIGGER trg_rs_edge_citation_counts;


--
-- Name: case_law case_law_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.case_law
    ADD CONSTRAINT case_law_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.legal_case(id);


--
-- Name: case_law case_law_law_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.case_law
    ADD CONSTRAINT case_law_law_id_fkey FOREIGN KEY (law_id) REFERENCES public.law_element(id);


--
-- Name: echr_document_appno echr_document_appno_itemid_languageisocode_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document_appno
    ADD CONSTRAINT echr_document_appno_itemid_languageisocode_fkey FOREIGN KEY (itemid, languageisocode) REFERENCES public.echr_document(itemid, languageisocode) ON DELETE CASCADE;


--
-- Name: echr_document_article echr_document_article_itemid_languageisocode_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document_article
    ADD CONSTRAINT echr_document_article_itemid_languageisocode_fkey FOREIGN KEY (itemid, languageisocode) REFERENCES public.echr_document(itemid, languageisocode) ON DELETE CASCADE;


--
-- Name: ecli_segments ecli_segments_ecli_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_segments
    ADD CONSTRAINT ecli_segments_ecli_fkey FOREIGN KEY (ecli) REFERENCES public.ecli_texts(ecli) ON DELETE CASCADE;


--
-- Name: ecli_keywords ecli_summary_keywords_ecli_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecli_keywords
    ADD CONSTRAINT ecli_summary_keywords_ecli_fkey FOREIGN KEY (ecli) REFERENCES public.ecli_texts(ecli) ON DELETE CASCADE;


--
-- Name: echr_document_text fk_echr_document_text__document; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_document_text
    ADD CONSTRAINT fk_echr_document_text__document FOREIGN KEY (itemid, languageisocode) REFERENCES public.echr_document(itemid, languageisocode) ON DELETE CASCADE;


--
-- Name: echr_extractor_segments fk_echr_extractor_segments_document; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.echr_extractor_segments
    ADD CONSTRAINT fk_echr_extractor_segments_document FOREIGN KEY (itemid, languageisocode) REFERENCES public.echr_document(itemid, languageisocode) ON DELETE CASCADE;


--
-- Name: rs_document_external_authority rs_document_external_authority_ecli_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_external_authority
    ADD CONSTRAINT rs_document_external_authority_ecli_fkey FOREIGN KEY (ecli) REFERENCES public.rs_document(ecli) ON DELETE CASCADE;


--
-- Name: rs_document_formal_relation rs_document_formal_relation_ecli_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_formal_relation
    ADD CONSTRAINT rs_document_formal_relation_ecli_fkey FOREIGN KEY (ecli) REFERENCES public.rs_document(ecli) ON DELETE CASCADE;


--
-- Name: rs_document_law_reference rs_document_law_reference_ecli_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_law_reference
    ADD CONSTRAINT rs_document_law_reference_ecli_fkey1 FOREIGN KEY (ecli) REFERENCES public.rs_document(ecli) ON DELETE CASCADE;


--
-- Name: rs_document_publication rs_document_publication_ecli_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_publication
    ADD CONSTRAINT rs_document_publication_ecli_fkey FOREIGN KEY (ecli) REFERENCES public.rs_document(ecli) ON DELETE CASCADE;


--
-- Name: rs_document_text rs_document_text_ecli_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rs_document_text
    ADD CONSTRAINT rs_document_text_ecli_fkey FOREIGN KEY (ecli) REFERENCES public.rs_document(ecli) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict yIYlQSmTHx4N2tP1aSlgw5Pn4RWTyYzcmyni2WG9gL9oFZ9zjZrS1Scq1VJtkRu

