from aws_cdk import (
    CfnOutput,
    Stack,
    aws_cognito as cognito,
)
from constructs import Construct


class CognitoSamlAuth(Construct):
    """
    Cognito User Pool with SAML IdP federation.

    Outputs the SP metadata URL and ACS URL for sharing with your IAM team
    so they can register this app as a SAML Service Provider in their IdP.

    Required config.yaml fields:
        cognito_domain_prefix   - globally unique prefix for the Cognito hosted UI domain
        saml_idp_name           - friendly name for the IdP (e.g. "CorporateSSO")
        saml_idp_metadata_url   - IdP metadata URL provided by your IAM team
        saml_attribute_mapping  - dict mapping Cognito attribute names to SAML attribute names

    Optional:
        cloudfront_url          - CloudFront distribution URL (auto-derived from frontend stack)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cognito_domain_prefix: str,
        saml_idp_name: str = None,
        saml_idp_metadata_url: str = None,
        saml_attribute_mapping: dict = None,
        dev_user_email: str = None,
        cloudfront_url: str = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Callback and logout URLs for the Cognito App Client.
        # Cognito redirects here after a successful login / logout.
        # Only the frontend's public URL (custom domain if set, else CloudFront)
        # is registered. localhost is added in dev mode only, so a production
        # pool never accepts logins from a locally-run frontend.
        callback_urls = []
        logout_urls = []
        if cloudfront_url:
            base = cloudfront_url.rstrip("/")
            callback_urls.append(base)
            logout_urls.append(base)
        if dev_user_email:
            callback_urls.append("http://localhost:5173")
            logout_urls.append("http://localhost:5173")

        # ------------------------------------------------------------------
        # User Pool
        # ------------------------------------------------------------------
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="chatbot-user-pool",
            # Lite tier: cheapest plan, supports SAML federation, 10K MAU free
            feature_plan=cognito.FeaturePlan.LITE,
            # Users sign in via SAML only — disable self-registration
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            # Standard attributes auto-verified via SAML assertion
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                given_name=cognito.StandardAttribute(required=False, mutable=True),
                family_name=cognito.StandardAttribute(required=False, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
        )

        # ------------------------------------------------------------------
        # SAML Identity Provider
        # ------------------------------------------------------------------
        # Build attribute mapping from config dict.
        # Keys are Cognito standard attribute names; values are SAML claim URIs.
        # SAML is skipped only when a dev user is configured and no metadata URL is set
        saml_idp = None
        if saml_idp_metadata_url:
            attr_map = {}
            for cognito_attr, saml_attr in (saml_attribute_mapping or {}).items():
                attr_map[cognito_attr] = cognito.ProviderAttribute.other(saml_attr)

            saml_idp = cognito.UserPoolIdentityProviderSaml(
                self,
                "SamlIdP",
                user_pool=self.user_pool,
                name=saml_idp_name,
                metadata=cognito.UserPoolIdentityProviderSamlMetadata.url(
                    saml_idp_metadata_url
                ),
                attribute_mapping=cognito.AttributeMapping(**attr_map),
                # Require signed SAML assertions (recommended for production)
                request_signing_algorithm=cognito.SigningAlgorithm.RSA_SHA256,
            )

        identity_providers = []
        if saml_idp:
            identity_providers.append(
                cognito.UserPoolClientIdentityProvider.custom(saml_idp_name)
            )
        if dev_user_email:
            # Dev only: enables Cognito username/password login on the hosted UI
            identity_providers.append(cognito.UserPoolClientIdentityProvider.COGNITO)

        # ------------------------------------------------------------------
        # App Client
        # ------------------------------------------------------------------
        self.app_client = cognito.UserPoolClient(
            self,
            "AppClient",
            user_pool=self.user_pool,
            user_pool_client_name="chatbot-app-client",
            # Authorization code flow — tokens stay server-side
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            supported_identity_providers=identity_providers,
            # Do not generate a client secret (SPA/Lambda@Edge can't store it securely)
            generate_secret=False,
        )

        # Ensure the IdP is created before the client references it
        if saml_idp:
            self.app_client.node.add_dependency(saml_idp)
            # Read by scripts/deploy-frontend.sh; absent in dev mode so the
            # frontend shows the hosted UI login form instead of forcing SAML.
            CfnOutput(
                self,
                "CognitoSamlIdpName",
                value=saml_idp_name,
                description="SAML identity provider name used for login redirect",
            )

        # ------------------------------------------------------------------
        # Optional dev user (dev stacks only; app.py never passes this in SAML mode)
        # ------------------------------------------------------------------
        # Cognito emails a temporary password to dev_user_email and forces a
        # password change on first sign-in. No password is stored in code or
        # the template, so the email must be a real inbox.
        if dev_user_email:
            cognito.CfnUserPoolUser(
                self,
                "DevUser",
                user_pool_id=self.user_pool.user_pool_id,
                username=dev_user_email,
                desired_delivery_mediums=["EMAIL"],
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=dev_user_email
                    ),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
            )

        # ------------------------------------------------------------------
        # Cognito Domain (Hosted UI)
        # ------------------------------------------------------------------
        self.domain = self.user_pool.add_domain(
            "CognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cognito_domain_prefix
            ),
        )

        # ------------------------------------------------------------------
        # Outputs — share these with your IAM team
        # ------------------------------------------------------------------
        hosted_ui_base = f"https://{cognito_domain_prefix}.auth.{Stack.of(self).region}.amazoncognito.com"

        CfnOutput(
            self,
            "CognitoUserPoolId",
            value=self.user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )

        CfnOutput(
            self,
            "CognitoHostedUiDomain",
            value=hosted_ui_base,
            description="Cognito hosted UI base URL (read by scripts/deploy-frontend.sh)",
        )

        CfnOutput(
            self,
            "CognitoAppClientId",
            value=self.app_client.user_pool_client_id,
            description="Cognito App Client ID",
        )

        CfnOutput(
            self,
            "SAMLAcsUrl",
            value=f"https://{cognito_domain_prefix}.auth.{Stack.of(self).region}.amazoncognito.com/saml2/idpresponse",
            description="SAML ACS URL — give this to your IAM team",
        )

        CfnOutput(
            self,
            "SAMLEntityId",
            value=f"urn:amazon:cognito:sp:{self.user_pool.user_pool_id}",
            description="SAML SP Entity ID — give this to your IAM team (available after deploy)",
        )

        CfnOutput(
            self,
            "SPMetadataUrl",
            value=f"https://{cognito_domain_prefix}.auth.{Stack.of(self).region}.amazoncognito.com/saml2/metadata",
            description="SP Metadata URL — your IAM team can import this directly",
        )

        # Expose for use by other constructs (e.g. Lambda@Edge)
        self.user_pool_id = self.user_pool.user_pool_id
        self.client_id = self.app_client.user_pool_client_id
        self.hosted_ui_base = hosted_ui_base
