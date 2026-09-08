/** Apply benchmark sampling controls inside DSH without an HTTP relay. */

export const name = 'harbor-dsh-sampling'
export const inject = ['deepseekLlmApiExtensions']

function requiredNumber(name, { minimum, maximum, exclusiveMinimum = false }) {
  const raw = process.env[name]
  const value = Number(raw)
  const belowMinimum = exclusiveMinimum ? value <= minimum : value < minimum
  if (raw === undefined || raw.trim() === '' || !Number.isFinite(value)
    || belowMinimum || (maximum !== undefined && value > maximum)) {
    throw new Error(`harbor-dsh-sampling: invalid ${name}`)
  }
  return value
}

export function apply(ctx) {
  const temperature = requiredNumber('DSH_TEMPERATURE', { minimum: 0 })
  const topP = requiredNumber('DSH_TOP_P', {
    minimum: 0,
    maximum: 1,
    exclusiveMinimum: true,
  })
  ctx.on('agent/request', async (_payload, next) => ({
    ...await next(),
    temperature,
  }))

  ctx.deepseekLlmApiExtensions.register('top_p', {
    prepare() {
      return {
        value: topP,
        accept() {},
      }
    },
  })
}
