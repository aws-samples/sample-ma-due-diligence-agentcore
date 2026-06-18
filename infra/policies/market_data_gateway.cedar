// Cedar policy for the M&A Due Diligence AgentCore Gateway.
//
// Restricts the market-data tool (get_comparable_multiples) to
// transportation-related industries only. Uses `like` with wildcard
// patterns (*) for flexible substring matching.
//
// The application layer normalizes industry_code to lowercase before
// calling the Gateway, so only lowercase patterns are needed here.
//
// Allowed examples:
//   "transportation", "logistics", "trucking",
//   "transportation-logistics", "transport", "air-transport-services"
//
// Denied examples:
//   "healthcare", "fintech", "retail"
//
// NOTE: The {{GATEWAY_ARN}} placeholder is replaced at deploy time
// by the CDK stack with the actual Gateway ARN.

permit(
  principal,
  action == AgentCore::Action::"market-data___get_comparable_multiples",
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when {
  context.input.industry_code like "*transport*" ||
  context.input.industry_code like "*logistic*" ||
  context.input.industry_code like "*truck*"
};
