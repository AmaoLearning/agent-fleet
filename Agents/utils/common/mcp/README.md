# Exa Web MCP
Build the stdio proxy before Agent Fleet starts:
```bash
python3 -m zipapp Agents/utils/common/mcp/exa -o /data/exa-web-mcp.pyz
```
Configure existing S3/Claude integration; replace `anonymous` with a paid key when needed:
```bash
export HARBOR_CC_WEB_MCP_SOURCE=/data/exa-web-mcp.pyz
export HARBOR_CC_WEB_MCP_MOUNT_PATH=/opt/agent-fleet/exa-web-mcp.pyz
export EXA_API_KEY=anonymous
```
