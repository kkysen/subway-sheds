import type { CustomLayerInterface, CustomRenderMethodInput } from "maplibre-gl";

// The grid of cells, in Web Mercator meters, as the pipeline writes it.
export interface BandsGrid {
  readonly x0: number;
  readonly y0: number;
  readonly spacing: number;
}

// Each cell's class is stored plus 1, so that 0 is a cell with nothing to show.
// The most there can be, counting nothing.
const SLOTS = 16;

export const EARTH_CIRCUMFERENCE = 2 * Math.PI * 6378137;

const FILL_OPACITY = 0.6;

// With a class highlighted, it's drawn stronger and the rest fainter,
// so that it stands out by more than its color.
const HIGHLIGHTED_OPACITY = 0.85;
const UNHIGHLIGHTED_OPACITY = 0.2;

// Projected with MapLibre's own `projectTile`, which its prelude for the map's projection defines.
const vertexSource = (shaderData: CustomRenderMethodInput["shaderData"]): string => `#version 300 es
${shaderData.vertexShaderPrelude}
${shaderData.define}
layout(location = 0) in vec2 a_mercator;
layout(location = 1) in vec2 a_cell;
out vec2 v_cell;
void main() {
  v_cell = a_cell;
  gl_Position = projectTile(a_mercator);
}
`;

// Each class's amount at a point is how much of it the cells around are,
// blurred by 1-2-1 across and then up, so that edges round off rather than run cell by cell,
// then interpolated between the cells' centers.
// Both together come to weighing the 4 by 4 cells around the point.
// The point goes to the class it's most of, which nothing competes for too,
// so that the bands meet without gaps or overlaps.
const fragmentSource = `#version 300 es
precision highp float;
precision highp int;
precision highp usampler2D;
uniform usampler2D u_classes;
uniform vec3 u_colors[${SLOTS}];
// The slot highlighted, or 0 for none.
uniform int u_highlight;
in vec2 v_cell;
out vec4 fragColor;

float opacity(int slot) {
  if (u_highlight == 0) {
    return ${FILL_OPACITY};
  }
  return slot == u_highlight ? ${HIGHLIGHTED_OPACITY} : ${UNHIGHLIGHTED_OPACITY};
}

void main() {
  ivec2 size = textureSize(u_classes, 0);
  vec2 position = clamp(v_cell, vec2(0.0), vec2(size - 1));
  ivec2 low = min(ivec2(floor(position)), max(size - 2, ivec2(0)));
  vec2 t = position - vec2(low);
  vec4 across = vec4(1.0 - t.x, 2.0 * (1.0 - t.x) + t.x, (1.0 - t.x) + 2.0 * t.x, t.x) / 4.0;
  vec4 up = vec4(1.0 - t.y, 2.0 * (1.0 - t.y) + t.y, (1.0 - t.y) + 2.0 * t.y, t.y) / 4.0;

  float amounts[${SLOTS}];
  for (int i = 0; i < ${SLOTS}; i++) {
    amounts[i] = 0.0;
  }
  for (int dy = 0; dy < 4; dy++) {
    for (int dx = 0; dx < 4; dx++) {
      ivec2 at = clamp(low + ivec2(dx - 1, dy - 1), ivec2(0), size - 1);
      amounts[int(texelFetch(u_classes, at, 0).r)] += across[dx] * up[dy];
    }
  }

  int most = 0;
  int next = 0;
  float mostAmount = -1.0;
  float nextAmount = -1.0;
  for (int i = 0; i < ${SLOTS}; i++) {
    float amount = amounts[i];
    if (amount > mostAmount) {
      next = most;
      nextAmount = mostAmount;
      most = i;
      mostAmount = amount;
    } else if (amount > nextAmount) {
      next = i;
      nextAmount = amount;
    }
  }

  // How far the point is, in pixels, from the edge of its class, where the next catches up.
  float ahead = mostAmount - nextAmount;
  float pixels = ahead / max(length(vec2(dFdx(ahead), dFdy(ahead))), 1e-6);

  // Each side of an edge blends into the other over a pixel, so that it's smooth.
  vec4 mostColor = most == 0 ? vec4(0.0) : vec4(u_colors[most], 1.0) * opacity(most);
  vec4 nextColor = next == 0 ? vec4(0.0) : vec4(u_colors[next], 1.0) * opacity(next);
  vec4 fill = mix(nextColor, mostColor, clamp(0.5 + pixels, 0.5, 1.0));

  // The edges, 0.75 pixels across, so that each band stands out from the next.
  // Where two bands meet, each one's outline was drawn, so they're darker there.
  float edgeOpacity = most != 0 && next != 0 ? 1.0 - 0.75 * 0.75 : 0.25;
  float edge = edgeOpacity * clamp(0.375 + 0.5 - pixels, 0.0, 1.0);

  // The highlighted band's edge, 3 pixels across and opaque, so that it's clear which it is,
  // from how far the point is from it, measured from that band alone,
  // so that it's unbroken where other bands meet it too.
  if (u_highlight != 0) {
    float rest = -1.0;
    for (int i = 0; i < ${SLOTS}; i++) {
      if (i != u_highlight) {
        rest = max(rest, amounts[i]);
      }
    }
    float inside = amounts[u_highlight] - rest;
    float fromEdge = abs(inside) / max(length(vec2(dFdx(inside), dFdy(inside))), 1e-6);
    edge = max(edge, clamp(1.5 + 0.5 - fromEdge, 0.0, 1.0));
  }
  fragColor = vec4(0.0, 0.0, 0.0, edge) + fill * (1.0 - edge);
}
`;

function compile(gl: WebGL2RenderingContext, type: GLenum, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (shader === null) {
    throw new Error("couldn't create a shader");
  }
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (gl.getShaderParameter(shader, gl.COMPILE_STATUS) !== true) {
    throw new Error(`couldn't compile a shader: ${gl.getShaderInfoLog(shader) ?? ""}`);
  }
  return shader;
}

function link(
  gl: WebGL2RenderingContext,
  shaderData: CustomRenderMethodInput["shaderData"],
): WebGLProgram {
  const program = gl.createProgram();
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, vertexSource(shaderData)));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, fragmentSource));
  gl.linkProgram(program);
  if (gl.getProgramParameter(program, gl.LINK_STATUS) !== true) {
    throw new Error(`couldn't link the bands' shaders: ${gl.getProgramInfoLog(program) ?? ""}`);
  }
  return program;
}

function rgb(color: string): [number, number, number] {
  const value = Number.parseInt(color.slice(1), 16);
  return [(value >> 16) / 255, ((value >> 8) & 0xff) / 255, (value & 0xff) / 255];
}

// The bands of travel time or minutes saved, drawn smooth on the GPU from each cell's class,
// so that picking a station costs only uploading its cells.
export class BandsLayer implements CustomLayerInterface {
  readonly type = "custom";
  readonly renderingMode = "2d";
  private gl: WebGL2RenderingContext | undefined;
  // Compiled for each projection the map is drawn in, by its variant's name.
  private readonly programs = new Map<string, WebGLProgram>();
  private vertexArray: WebGLVertexArrayObject | undefined;
  private texture: WebGLTexture | undefined;
  private colors = new Float32Array(SLOTS * 3);
  private highlight = 0;
  private triggerRepaint = (): void => {};

  readonly id: string;
  private readonly grid: BandsGrid;
  private readonly width: number;
  private readonly height: number;

  constructor(id: string, grid: BandsGrid, width: number, height: number) {
    this.id = id;
    this.grid = grid;
    this.width = width;
    this.height = height;
  }

  onAdd(map: { triggerRepaint(): void }, gl: WebGL2RenderingContext): void {
    this.gl = gl;
    this.triggerRepaint = () => map.triggerRepaint();

    // A quad over the grid, from the edges of its outermost cells,
    // in Mercator coordinates from 0 to 1, and in cells, with cell centers at whole numbers.
    const { x0, y0, spacing } = this.grid;
    const corners = [
      [-0.5, -0.5],
      [this.width - 0.5, -0.5],
      [-0.5, this.height - 0.5],
      [this.width - 0.5, this.height - 0.5],
    ];
    const vertices = new Float32Array(
      corners.flatMap(([col = 0, row = 0]) => [
        0.5 + (x0 + col * spacing) / EARTH_CIRCUMFERENCE,
        0.5 - (y0 + row * spacing) / EARTH_CIRCUMFERENCE,
        col,
        row,
      ]),
    );
    this.vertexArray = gl.createVertexArray();
    gl.bindVertexArray(this.vertexArray);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 16, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 16, 8);
    gl.bindVertexArray(null);

    this.texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texStorage2D(gl.TEXTURE_2D, 1, gl.R8UI, this.width, this.height);
    this.setClasses(new Int8Array(this.width * this.height).fill(-1), []);
  }

  // Each cell's class, the index of its color in `colors`, or -1 for nothing to show,
  // in rows from the south, `width` cells to a row.
  setClasses(classes: Int8Array, colors: readonly string[]): void {
    const gl = this.gl;
    if (gl === undefined) {
      return;
    }
    if (colors.length >= SLOTS) {
      throw new Error(`${colors.length} classes, but room for only ${SLOTS - 1}`);
    }
    const slots = new Uint8Array(classes.length);
    for (let i = 0; i < classes.length; i++) {
      slots[i] = (classes[i] ?? -1) + 1;
    }
    gl.bindTexture(gl.TEXTURE_2D, this.texture ?? null);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texSubImage2D(
      gl.TEXTURE_2D,
      0,
      0,
      0,
      this.width,
      this.height,
      gl.RED_INTEGER,
      gl.UNSIGNED_BYTE,
      slots,
    );
    this.colors.fill(0);
    colors.forEach((color, i) => {
      this.colors.set(rgb(color), (i + 1) * 3);
    });
    this.triggerRepaint();
  }

  // The class to highlight, as in `setClasses`, or -1 for none.
  setHighlight(highlight: number): void {
    if (highlight + 1 !== this.highlight) {
      this.highlight = highlight + 1;
      this.triggerRepaint();
    }
  }

  render(gl: WebGL2RenderingContext, options: CustomRenderMethodInput): void {
    const { shaderData, defaultProjectionData: projection } = options;
    let program = this.programs.get(shaderData.variantName);
    if (program === undefined) {
      program = link(gl, shaderData);
      this.programs.set(shaderData.variantName, program);
    }
    gl.useProgram(program);
    const uniform = (name: string) => gl.getUniformLocation(program, name);
    gl.uniformMatrix4fv(uniform("u_projection_matrix"), false, projection.mainMatrix);
    gl.uniformMatrix4fv(uniform("u_projection_fallback_matrix"), false, projection.fallbackMatrix);
    gl.uniform4f(uniform("u_projection_tile_mercator_coords"), ...projection.tileMercatorCoords);
    gl.uniform4f(uniform("u_projection_clipping_plane"), ...projection.clippingPlane);
    gl.uniform1f(uniform("u_projection_transition"), projection.projectionTransition);
    gl.uniform1i(uniform("u_projection_clip_antimeridian"), projection.clipAntimeridian ? 1 : 0);
    gl.uniform3fv(uniform("u_colors"), this.colors);
    gl.uniform1i(uniform("u_highlight"), this.highlight);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.texture ?? null);
    gl.uniform1i(uniform("u_classes"), 0);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.STENCIL_TEST);
    gl.disable(gl.CULL_FACE);
    // Premultiplied, as MapLibre blends.
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.vertexArray ?? null);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.bindVertexArray(null);
  }

  onRemove(_map: unknown, gl: WebGL2RenderingContext): void {
    for (const program of this.programs.values()) {
      gl.deleteProgram(program);
    }
    gl.deleteVertexArray(this.vertexArray ?? null);
    gl.deleteTexture(this.texture ?? null);
  }
}
