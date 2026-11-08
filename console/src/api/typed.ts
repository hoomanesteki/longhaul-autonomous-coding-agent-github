/**
 * Types of a call, worked out from the paths of the generated schema.
 *
 * A call names a method and a path of the API. The path has to be a key of `paths`, the method
 * has to be one that path has, and the options are what the schema says they are: `path` when
 * the path has parameters, `query` when the operation has a query, `body` when it takes one.
 * An option the schema marks as required is required here too. So when the API renames a path,
 * moves a parameter, or makes a body or a query parameter required, the calls that no longer fit
 * fail `npm run typecheck`.
 *
 * The types take the spec as a parameter (`S`) so that a test can check the rules against a spec
 * of its own. The client uses the generated `paths`.
 */

export type Method = "get" | "post";

type None = Record<never, never>;

/** The operation a path has for a method, or `never` when it has none. */
type OpOf<S, P extends keyof S, M extends Method> =
  S[P] extends { [K in M]?: infer O } ? NonNullable<O> : never;

/** The paths of the spec that have an operation for the method. */
export type PathsWith<S, M extends Method> = {
  [P in keyof S]: [OpOf<S, P, M>] extends [never] ? never : P;
}[keyof S];

type JsonOf<T> = T extends { content: { "application/json": infer B } } ? B : never;

type ParametersOf<O> = O extends { parameters: infer P } ? P : never;

/** The type of a field, or `never` when the holder has no such field. */
type Field<Holder, K extends string> = K extends keyof Holder ? Holder[K] : never;

/** True when the schema marks the field with `?`, and for a field that is not there. */
type IsOptional<Holder, K extends string> = K extends keyof Holder
  ? None extends Pick<Holder, K>
    ? true
    : false
  : true;

/**
 * One option. It is absent when the schema has nothing for it, optional when the schema says
 * `?` and required otherwise.
 */
type Slot<Name extends string, Holder, K extends string, Value> = [
  NonNullable<Field<Holder, K>>,
] extends [never]
  ? None
  : IsOptional<Holder, K> extends true
    ? { [N in Name]?: Value }
    : { [N in Name]: Value };

type PathOf<O> = NonNullable<Field<ParametersOf<O>, "path">>;
type QueryIn<O> = NonNullable<Field<ParametersOf<O>, "query">>;
type BodyIn<O> = JsonOf<NonNullable<Field<O, "requestBody">>>;

export type Options<S, P extends keyof S, M extends Method> = Slot<
  "path",
  ParametersOf<OpOf<S, P, M>>,
  "path",
  PathOf<OpOf<S, P, M>>
> &
  Slot<"query", ParametersOf<OpOf<S, P, M>>, "query", QueryIn<OpOf<S, P, M>>> &
  Slot<"body", OpOf<S, P, M>, "requestBody", BodyIn<OpOf<S, P, M>>>;

/** The rest arguments of a call: the options, which may be left out only if all of them are optional. */
export type Args<S, P extends keyof S, M extends Method> =
  None extends Options<S, P, M> ? [options?: Options<S, P, M>] : [options: Options<S, P, M>];

/** The body of the 200 reply of an operation. */
export type Reply<S, P extends keyof S, M extends Method> =
  OpOf<S, P, M> extends { responses: { 200: infer R } } ? JsonOf<R> : never;

/** The query an operation takes, for a wrapper that passes it on. */
export type QueryOf<S, P extends keyof S, M extends Method> = QueryIn<OpOf<S, P, M>>;

/** The JSON body an operation takes, for a wrapper that passes it on. */
export type BodyOf<S, P extends keyof S, M extends Method> = BodyIn<OpOf<S, P, M>>;
