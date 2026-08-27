export const name = 'agent-fleet-dsh-sampling'

export function apply(ctx, config = {}) {
  const temperature = Number(config.temperature)
  if (!Number.isFinite(temperature)) {
    throw new TypeError('agent-fleet-dsh-sampling: temperature must be finite')
  }

  ctx.on('agent/request', async (_payload, next) => ({
    ...(await next()),
    temperature,
  }))
}
