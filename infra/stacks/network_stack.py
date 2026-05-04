"""NetworkStack - VPC and networking primitives for the sample.

This stack owns the private networking fabric that hosts Aurora PostgreSQL
Serverless v2, the Gateway-backed Lambdas, and any Custom Resource
Lambdas that need to reach AWS APIs from inside the VPC.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
section *Infrastructure as Code Design - NetworkStack*.

Requirements implemented by this stack:

- **14.6** Aurora PostgreSQL is deployed into private subnets with no
  public internet path. The two ``PRIVATE_ISOLATED`` subnets created
  below are exactly what :class:`infra.stacks.data_stack.DataStack`
  attaches the Aurora cluster to. VPC interface endpoints for Secrets
  Manager and the RDS Data API, plus a gateway endpoint for S3, let
  workloads reach the AWS control plane without needing an internet
  gateway or NAT egress for those services.

Notes on the layout:

- Two Availability Zones keep the footprint cheap (requirement 13
  targets under $5 per deploy-run-cleanup cycle) while still providing
  the multi-AZ subnet pair Aurora Serverless v2 demands.
- A single shared NAT gateway backs the ``PRIVATE_WITH_EGRESS`` subnets
  so Lambdas that genuinely need outbound internet access (for example
  fetching a base image or pulling a public dependency during cold
  start) have a path without multiplying NAT cost across AZs. The NAT
  gateway itself lives in a small ``PUBLIC`` subnet tier that exists
  only to host the NAT — no workloads are placed there, keeping the
  "no public internet path to Aurora" invariant intact.
- The *Aurora security group* exported from this stack is the single
  ingress boundary for the database. :class:`DataStack` attaches it to
  the cluster, and downstream stacks call ``grant_aurora_ingress`` to
  authorize specific client security groups (the agent runtime, the
  bootstrap Custom Resource, etc.).
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

# CIDR chosen to not collide with common corporate defaults while still
# leaving plenty of room for future expansion (65k addresses).
_VPC_CIDR = "10.0.0.0/16"

# /24 per subnet gives 251 usable addresses per subnet — ample for the
# sample's Lambda fan-out plus Aurora, and keeps subnet math easy to
# eyeball during troubleshooting.
_SUBNET_CIDR_MASK = 24


class NetworkStack(Stack):
    """VPC, subnets, and VPC endpoints for the M&A sample.

    Public attributes consumed by downstream stacks:

    - :attr:`vpc` - the shared :class:`aws_cdk.aws_ec2.Vpc`.
    - :attr:`aurora_subnet_selection` - the two ``PRIVATE_ISOLATED``
      subnets that host the Aurora cluster (requirement 14.6).
    - :attr:`private_with_egress_subnet_selection` - the two
      ``PRIVATE_WITH_EGRESS`` subnets for Lambdas that need NAT.
    - :attr:`aurora_security_group` - security group attached to the
      Aurora cluster; downstream stacks authorize ingress via
      :meth:`grant_aurora_ingress`.
    """

    # Logical subnet group names. These must line up with the
    # ``subnet_group_name`` values used below so the ``SubnetSelection``
    # helpers resolve to the right subnets even after additional
    # subnet configurations are added later.
    AURORA_SUBNET_GROUP = "AuroraIsolated"
    EGRESS_SUBNET_GROUP = "PrivateWithEgress"
    # Public subnet tier exists solely to host the shared NAT gateway.
    # No workloads are ever placed here; it is an implementation detail
    # of providing egress to the ``PRIVATE_WITH_EGRESS`` subnets.
    PUBLIC_SUBNET_GROUP = "PublicNatEgress"

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # VPC + subnets (requirement 14.6)
        # ------------------------------------------------------------------
        # Two AZs with paired isolated + egress subnets. The
        # ``PRIVATE_ISOLATED`` tier has no route to the internet at all
        # — this is the control that satisfies "no public internet
        # access" for Aurora. The ``PRIVATE_WITH_EGRESS`` tier is for
        # Lambdas that need to reach public AWS endpoints that are not
        # covered by our VPC endpoints (for example the Bedrock control
        # plane during Custom Resource execution).
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(_VPC_CIDR),
            max_azs=2,
            nat_gateways=1,  # One shared NAT keeps idle cost bounded.
            subnet_configuration=[
                # Public tier exists only to host the NAT gateway.
                # ``map_public_ip_on_launch=False`` prevents any
                # accidental workload deployment from getting a public
                # IP — the NAT gateway itself does not need one on the
                # ENI, only the NAT service does.
                ec2.SubnetConfiguration(
                    name=self.PUBLIC_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=_SUBNET_CIDR_MASK,
                    map_public_ip_on_launch=False,
                ),
                ec2.SubnetConfiguration(
                    name=self.AURORA_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=_SUBNET_CIDR_MASK,
                ),
                ec2.SubnetConfiguration(
                    name=self.EGRESS_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=_SUBNET_CIDR_MASK,
                ),
            ],
        )

        # Pre-built selections downstream stacks can pass directly into
        # ``vpc_subnets=`` parameters — saves every caller from
        # re-deriving the selection from subnet group names.
        self.aurora_subnet_selection = ec2.SubnetSelection(
            subnet_group_name=self.AURORA_SUBNET_GROUP,
        )
        self.private_with_egress_subnet_selection = ec2.SubnetSelection(
            subnet_group_name=self.EGRESS_SUBNET_GROUP,
        )

        # ------------------------------------------------------------------
        # Aurora security group (requirement 14.6)
        # ------------------------------------------------------------------
        # Default egress is allowed so the cluster can reach the AWS
        # control plane for maintenance events. Ingress stays empty
        # here; :meth:`grant_aurora_ingress` is the sanctioned path for
        # downstream stacks to open PostgreSQL (TCP/5432) from their
        # own client security groups, keeping the ingress rules
        # auditable in one place.
        self.aurora_security_group = ec2.SecurityGroup(
            self,
            "AuroraSecurityGroup",
            vpc=self.vpc,
            description="Aurora PostgreSQL ingress boundary for the M&A sample",
            allow_all_outbound=True,
        )

        # ------------------------------------------------------------------
        # VPC endpoints (requirement 14.6)
        # ------------------------------------------------------------------
        # Interface endpoints live in the egress subnets so both Aurora
        # (from the isolated subnets via route table entries) and
        # Lambdas (from the egress subnets directly) can reach them.
        # A dedicated security group lets us allow HTTPS from the VPC
        # CIDR without punching holes in any workload security group.
        endpoint_sg = ec2.SecurityGroup(
            self,
            "VpcEndpointSecurityGroup",
            vpc=self.vpc,
            description="Allows HTTPS from the VPC CIDR to interface endpoints",
            allow_all_outbound=True,
        )
        endpoint_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from within the VPC to VPC interface endpoints",
        )

        endpoint_subnets = ec2.SubnetSelection(
            subnet_group_name=self.EGRESS_SUBNET_GROUP,
        )

        # Secrets Manager interface endpoint — Aurora credentials live
        # in Secrets Manager (task 6) and the agent runtime plus the
        # Aurora bootstrap Custom Resource (task 12) both need to
        # resolve them without traversing the internet.
        self.secrets_manager_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "SecretsManagerEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            subnets=endpoint_subnets,
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # RDS Data API interface endpoint — the agents access Aurora
        # exclusively through the RDS Data API (requirement 2a.5 +
        # design "Container Build Pipeline" rationale), so this
        # endpoint is the actual data path and must stay inside the
        # VPC. Service name ``com.amazonaws.{region}.rds-data`` is
        # represented by :attr:`InterfaceVpcEndpointAwsService.RDS_DATA`
        # in CDK v2.
        self.rds_data_endpoint = ec2.InterfaceVpcEndpoint(
            self,
            "RdsDataEndpoint",
            vpc=self.vpc,
            service=ec2.InterfaceVpcEndpointAwsService.RDS_DATA,
            subnets=endpoint_subnets,
            security_groups=[endpoint_sg],
            private_dns_enabled=True,
        )

        # S3 gateway endpoint — attached to the route tables of both
        # subnet groups so the documents bucket and the CodeBuild
        # source bucket are reachable from Aurora (for pgvector data
        # loads triggered by the KB) and from Lambdas (for reads/writes
        # during Custom Resource execution) without NAT egress cost.
        self.s3_gateway_endpoint = self.vpc.add_gateway_endpoint(
            "S3GatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[
                ec2.SubnetSelection(subnet_group_name=self.AURORA_SUBNET_GROUP),
                ec2.SubnetSelection(subnet_group_name=self.EGRESS_SUBNET_GROUP),
            ],
        )

        # ------------------------------------------------------------------
        # CloudFormation outputs for visibility (requirement 14.6)
        # ------------------------------------------------------------------
        # Exported by logical name so operators running ``aws
        # cloudformation describe-stacks`` can see the exact subnet and
        # security-group IDs Aurora is pinned to, which makes auditing
        # the "no public internet access" control straightforward.
        CfnOutput(
            self,
            "VpcIdOutput",
            value=self.vpc.vpc_id,
            description="Shared VPC for the M&A sample",
        )
        for idx, subnet in enumerate(self.vpc.isolated_subnets):
            CfnOutput(
                self,
                f"AuroraSubnetId{idx}",
                value=subnet.subnet_id,
                description=f"Private-isolated subnet {idx} hosting Aurora",
            )
        for idx, subnet in enumerate(self.vpc.private_subnets):
            CfnOutput(
                self,
                f"PrivateWithEgressSubnetId{idx}",
                value=subnet.subnet_id,
                description=f"Private-with-egress subnet {idx} for Lambdas",
            )
        CfnOutput(
            self,
            "AuroraSecurityGroupIdOutput",
            value=self.aurora_security_group.security_group_id,
            description="Security group attached to the Aurora cluster",
        )

    # ----------------------------------------------------------------------
    # Convenience helpers for downstream stacks
    # ----------------------------------------------------------------------
    def grant_aurora_ingress(
        self,
        client_security_group: ec2.ISecurityGroup,
        description: str,
    ) -> None:
        """Authorize a client security group to reach Aurora on 5432.

        Downstream stacks (DataStack for the bootstrap Lambda, AgentStack
        for the agent runtime) call this rather than manipulating the
        Aurora security group directly so every ingress rule lives in
        one reviewable place.
        """

        self.aurora_security_group.add_ingress_rule(
            peer=client_security_group,
            connection=ec2.Port.tcp(5432),
            description=description,
        )
