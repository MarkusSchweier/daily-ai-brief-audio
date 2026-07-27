"""CDK-synth-based assertions on the `/audio/*` CloudFront behavior + the pipeline
bucket's OAC bucket policy (PRD docs/prd/audio-link-delivery.md, ADR-0017 D2/D3/D7).

Because ADR-0017 D2 flipped the bucket-policy statement to be CDK-managed (against the
architect's out-of-band recommendation), this test covers the ENTIRE R1 surface -- the
routing rule AND the IAM boundary -- exactly as D7 calls for: "the concrete upside the
owner bought at the gate."

Two gotchas from the architect, both defended against explicitly below:
  - a policy statement with a SINGLE `Action` renders as a bare STRING in the synthesized
    template, never a one-element list -- asserted by equality, never iterated;
  - both the bucket ARN and the distribution ARN are CDK TOKENS at synth time (an
    `Fn::Join` of literal segments + a `{"Ref": ...}`), not plain strings -- matched via
    the `json.dumps`-substring helper `test_stack_iam.py` already establishes, not via a
    literal string comparison.
"""

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from brief_subscribers.stack import PIPELINE_BUCKET_NAME, BriefSubscribersStack


def _synth_template(context: dict | None = None) -> Template:
    app = cdk.App(context=context or {"subscribeDomainName": "briefing.mschweier.com"})
    stack = BriefSubscribersStack(
        app,
        "TestBriefSubscribersStack",
        env=cdk.Environment(account="740353583786", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _distribution_config(template: Template) -> dict:
    (dist_props,) = [
        r["Properties"] for r in template.find_resources("AWS::CloudFront::Distribution").values()
    ]
    return dist_props["DistributionConfig"]


def _origin_domain_by_id(config: dict, origin_id):
    """Returns the origin's `DomainName` -- a plain string for the imported pipeline
    bucket (its name is a literal, so CDK can compute the regional domain at synth
    time), but a TOKEN (`Fn::GetAtt`) for the CDK-owned site bucket (its bucket name
    is auto-generated). Callers must compare via JSON-substring containment, not
    `str` methods, to handle both shapes uniformly."""
    for origin in config["Origins"]:
        if origin["Id"] == origin_id:
            return origin["DomainName"]
    raise AssertionError(f"no origin with id {origin_id!r} in {config['Origins']!r}")


def _resource_ends_with(resource, suffix: str) -> bool:
    """True if `resource` (a plain ARN string, or an `Fn::Join` CDK token whose
    final element is a literal string) ends with `suffix`. Handles both shapes
    uniformly -- the ARNs this test suite inspects are tokens (an `Fn::Join` of
    literal segments plus a `{"Ref": ...}`/`{"Fn::GetAtt": ...}` piece), never plain
    strings, at synth time."""
    if isinstance(resource, str):
        return resource.endswith(suffix)
    if isinstance(resource, dict) and "Fn::Join" in resource:
        _delimiter, parts = resource["Fn::Join"]
        last = parts[-1] if parts else None
        if isinstance(last, str):
            return last.endswith(suffix)
    return False


# ---------------------------------------------------------------------------
# Routing side -- exactly one added behavior, PathPattern /audio/*, nothing broader,
# origin = pipeline bucket regional domain, default behavior unchanged, OAC present.
# ---------------------------------------------------------------------------


def test_exactly_one_additional_cache_behavior():
    template = _synth_template()
    config = _distribution_config(template)
    behaviors = config.get("CacheBehaviors", [])
    assert len(behaviors) == 1, f"expected exactly one additional cache behavior, found {len(behaviors)}"


def test_the_additional_behaviors_path_pattern_is_exactly_audio_star():
    template = _synth_template()
    config = _distribution_config(template)
    (behavior,) = config["CacheBehaviors"]
    assert behavior["PathPattern"] == "/audio/*"


def test_no_behavior_path_pattern_is_broader_than_audio_star():
    """R1: nothing routes a wider path (e.g. a bare `/*` or `/briefs/*`) to the
    pipeline bucket origin."""
    template = _synth_template()
    config = _distribution_config(template)
    path_patterns = [b["PathPattern"] for b in config.get("CacheBehaviors", [])]
    for pattern in path_patterns:
        assert pattern == "/audio/*", f"unexpected/broader behavior path pattern: {pattern!r}"


def test_the_audio_behaviors_origin_is_the_pipeline_buckets_regional_domain():
    template = _synth_template()
    config = _distribution_config(template)
    (behavior,) = config["CacheBehaviors"]
    domain = _origin_domain_by_id(config, behavior["TargetOriginId"])
    domain_json = json.dumps(domain)
    assert f"{PIPELINE_BUCKET_NAME}.s3." in domain_json
    assert ".amazonaws.com" in domain_json


def test_the_audio_behavior_allows_only_get_and_head():
    template = _synth_template()
    config = _distribution_config(template)
    (behavior,) = config["CacheBehaviors"]
    assert set(behavior["AllowedMethods"]) == {"GET", "HEAD"}
    assert set(behavior["CachedMethods"]) == {"GET", "HEAD"}


def test_the_audio_behavior_disables_compression():
    template = _synth_template()
    config = _distribution_config(template)
    (behavior,) = config["CacheBehaviors"]
    assert behavior["Compress"] is False


def test_the_audio_behavior_redirects_http_to_https():
    """ADR-0017 D3: 'Viewer protocol | REDIRECT_TO_HTTPS | Matches the default
    behavior.' -- gap found in review: no existing test asserted this, so a
    regression here (e.g. someone copy-pasting `ALLOW_ALL` for a debugging
    session) would ship silently. CDK's `REDIRECT_TO_HTTPS` renders as the
    literal string `redirect-to-https` in the synthesized template."""
    template = _synth_template()
    config = _distribution_config(template)
    (behavior,) = config["CacheBehaviors"]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"


def test_default_behavior_also_redirects_http_to_https():
    """Same check for the pre-existing default (site) behavior, so a future
    change can't silently weaken either behavior's transport security without
    a failing test."""
    template = _synth_template()
    config = _distribution_config(template)
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"


def test_default_behavior_still_points_at_the_site_bucket_not_the_pipeline_bucket():
    template = _synth_template()
    config = _distribution_config(template)
    default_domain = _origin_domain_by_id(config, config["DefaultCacheBehavior"]["TargetOriginId"])
    assert PIPELINE_BUCKET_NAME not in json.dumps(default_domain)


def test_origin_access_control_exists():
    template = _synth_template()
    oacs = template.find_resources("AWS::CloudFront::OriginAccessControl")
    assert len(oacs) >= 1
    for resource in oacs.values():
        config = resource["Properties"]["OriginAccessControlConfig"]
        assert config["OriginAccessControlOriginType"] == "s3"
        assert config["SigningBehavior"] == "always"


def test_audio_cache_policy_has_the_specified_ttls_and_forwards_nothing():
    template = _synth_template()
    (policy_props,) = [
        r["Properties"] for r in template.find_resources("AWS::CloudFront::CachePolicy").values()
    ]
    params = policy_props["CachePolicyConfig"]["ParametersInCacheKeyAndForwardedToOrigin"]
    assert policy_props["CachePolicyConfig"]["MinTTL"] == 0
    assert policy_props["CachePolicyConfig"]["DefaultTTL"] == 3600
    assert policy_props["CachePolicyConfig"]["MaxTTL"] == 86400
    assert params["CookiesConfig"]["CookieBehavior"] == "none"
    assert params["QueryStringsConfig"]["QueryStringBehavior"] == "none"
    assert params["HeadersConfig"]["HeaderBehavior"] == "none"
    assert params["EnableAcceptEncodingGzip"] is False
    assert params["EnableAcceptEncodingBrotli"] is False


def test_audio_response_headers_policy_sets_inline_content_disposition_with_override():
    template = _synth_template()
    (policy_props,) = [
        r["Properties"] for r in template.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()
    ]
    (header,) = policy_props["ResponseHeadersPolicyConfig"]["CustomHeadersConfig"]["Items"]
    assert header["Header"] == "Content-Disposition"
    assert header["Value"] == 'inline; filename="daily-ai-brief.mp3"'
    assert header["Override"] is True


# ---------------------------------------------------------------------------
# Policy side -- exactly one AWS::S3::BucketPolicy on PIPELINE_BUCKET_NAME; its
# document contains exactly one statement with the exact shape D2 specifies; a
# negative assertion pins that nothing broader than audio/* is ever granted.
# ---------------------------------------------------------------------------


def _pipeline_bucket_policies(template: Template) -> list[dict]:
    """Every AWS::S3::BucketPolicy resource whose Bucket is PIPELINE_BUCKET_NAME
    (as opposed to, e.g., the pre-existing SubscribeSiteBucket's own auto-added
    enforce_ssl policy, a DIFFERENT bucket policy resource in the same template)."""
    policies = template.find_resources("AWS::S3::BucketPolicy")
    return [
        resource
        for resource in policies.values()
        if resource["Properties"].get("Bucket") == PIPELINE_BUCKET_NAME
    ]


def test_exactly_one_bucket_policy_on_the_pipeline_bucket():
    template = _synth_template()
    matching = _pipeline_bucket_policies(template)
    assert len(matching) == 1, f"expected exactly one BucketPolicy on {PIPELINE_BUCKET_NAME}, found {len(matching)}"


def test_the_pipeline_bucket_policy_document_has_exactly_one_statement():
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    statements = resource["Properties"]["PolicyDocument"]["Statement"]
    assert len(statements) == 1


def test_the_statements_resource_ends_in_audio_star_and_equals_the_full_expected_arn():
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
    resource_json = json.dumps(statement["Resource"])
    assert f"{PIPELINE_BUCKET_NAME}/audio/*" in resource_json
    # No trailing content past the /audio/* prefix (a real ARN string, once resolved
    # from the Fn::Join, is exactly ".../audio/*" -- not ".../audio/*something").
    assert _resource_ends_with(statement["Resource"], "audio/*")


def test_the_statements_action_is_exactly_s3_get_object_a_bare_string():
    """A single-action PolicyStatement renders `Action` as a bare STRING, never a
    one-element list -- assert equality, never iterate it (architect's own gotcha)."""
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "s3:GetObject"


def test_the_statements_effect_is_allow():
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"


def test_the_statements_principal_is_the_cloudfront_service_principal():
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Principal"] == {"Service": "cloudfront.amazonaws.com"}


def test_the_statements_sid_is_the_ratified_sid():
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
    assert statement.get("Sid") == "AllowCloudFrontReadAudioPrefixOnly"


def test_the_condition_pins_this_one_distributions_source_arn():
    """The `AWS:SourceArn` condition must reference THIS stack's own distribution --
    proven here by requiring the distribution's own (tokenized) logical id to appear
    in the condition's resolved JSON, per the json.dumps-substring convention
    (both ARNs are CDK tokens, not plain strings, at synth time)."""
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
    condition = statement["Condition"]
    assert set(condition.keys()) == {"StringEquals"}
    source_arn = condition["StringEquals"]["AWS:SourceArn"]
    source_arn_json = json.dumps(source_arn)
    assert ":cloudfront::" in source_arn_json
    assert ":distribution/" in source_arn_json

    (dist_logical_id,) = template.find_resources("AWS::CloudFront::Distribution").keys()
    assert dist_logical_id in source_arn_json


def test_the_bucket_policy_is_destroyable_not_retained():
    template = _synth_template()
    (resource,) = _pipeline_bucket_policies(template)
    assert resource.get("DeletionPolicy", "Delete") == "Delete"


def test_no_statement_on_the_pipeline_bucket_grants_anything_outside_audio_star():
    """R1's negative assertion, scoped to RESOURCE-based (bucket) policies on this
    bucket -- the highest-consequence failure mode this feature could introduce.
    (Pre-existing IDENTITY-based grants elsewhere in the template, e.g.
    WelcomeSendFunctionRole's legitimate `briefs/*` read, are a different statement
    type entirely and are intentionally OUT of scope for this check -- see
    test_stack_iam.py for those.)"""
    template = _synth_template()
    for resource in _pipeline_bucket_policies(template):
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            resource_json = json.dumps(statement.get("Resource"))
            assert "briefs/" not in resource_json, f"bucket policy statement reaches briefs/: {statement!r}"
            # A bare wildcard resource (the whole bucket, or `bucket/*`) would grant
            # everything, not just audio/* -- reject anything that doesn't end in
            # exactly ".../audio/*".
            assert _resource_ends_with(statement.get("Resource"), "audio/*"), (
                f"bucket policy statement is not scoped to exactly audio/*: {statement!r}"
            )


def test_no_bucket_policy_statement_anywhere_grants_a_bare_bucket_wildcard_on_the_pipeline_bucket():
    """Belt-and-braces whole-template check: no `AWS::S3::BucketPolicy` resource
    anywhere references `PIPELINE_BUCKET_NAME` with a resource ARN that is the bare
    bucket or `bucket/*` (as opposed to the scoped `bucket/audio/*`)."""
    template = _synth_template()
    for resource in template.find_resources("AWS::S3::BucketPolicy").values():
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            resource_json = json.dumps(statement.get("Resource"))
            if PIPELINE_BUCKET_NAME not in resource_json:
                continue
            assert _resource_ends_with(statement.get("Resource"), "audio/*"), (
                f"unexpected non-audio/* grant on the pipeline bucket: {statement!r}"
            )
