"""3D mesh preview widget with a fast OpenGL path and optimized fallback.

The viewer prefers a hardware-accelerated ``QOpenGLWidget`` when the runtime
has ``PyOpenGL`` available. If that stack is missing or fails, it falls back to
an optimized software renderer that projects vertices once per frame and uses a
lighter interactive mode while dragging.
"""

from __future__ import annotations

import ctypes
import math
from array import array
from dataclasses import dataclass
import numpy as np

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QBrush, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import QWidget

try:
    from OpenGL.GL import (
        GL_ARRAY_BUFFER,
        GL_CLAMP_TO_EDGE,
        GL_COLOR_BUFFER_BIT,
        GL_COMPILE_STATUS,
        GL_DEPTH_BUFFER_BIT,
        GL_DEPTH_TEST,
        GL_ELEMENT_ARRAY_BUFFER,
        GL_FALSE,
        GL_FLOAT,
        GL_FRAGMENT_SHADER,
        GL_LINEAR,
        GL_LINEAR_MIPMAP_LINEAR,
        GL_LINK_STATUS,
        GL_MULTISAMPLE,
        GL_REPEAT,
        GL_RGBA,
        GL_RGBA8,
        GL_STATIC_DRAW,
        GL_TEXTURE_2D,
        GL_TEXTURE_MAG_FILTER,
        GL_TEXTURE_MIN_FILTER,
        GL_TEXTURE_WRAP_S,
        GL_TEXTURE_WRAP_T,
        GL_TEXTURE0,
        GL_TRUE,
        GL_TRIANGLES,
        GL_UNSIGNED_BYTE,
        GL_UNSIGNED_INT,
        GL_VERTEX_SHADER,
        glActiveTexture,
        glAttachShader,
        glBindBuffer,
        glBindTexture,
        glBindVertexArray,
        glBufferData,
        glClear,
        glClearColor,
        glCompileShader,
        glCreateProgram,
        glCreateShader,
        glDeleteShader,
        glDeleteTextures,
        glDrawArrays,
        glDrawElements,
        glEnable,
        glEnableVertexAttribArray,
        glGenBuffers,
        glGenTextures,
        glGenVertexArrays,
        glGenerateMipmap,
        glGetProgramInfoLog,
        glGetProgramiv,
        glGetShaderInfoLog,
        glGetShaderiv,
        glGetUniformLocation,
        glLinkProgram,
        glShaderSource,
        glTexImage2D,
        glTexParameteri,
        glUniform1i,
        glUniform3f,
        glUniform3fv,
        glUniformMatrix4fv,
        glUseProgram,
        glVertexAttribPointer,
        glViewport,
    )
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    _GL_RUNTIME_AVAILABLE = True
except Exception:
    QSurfaceFormat = None
    QOpenGLWidget = None
    _GL_RUNTIME_AVAILABLE = False


_VIEWER_HELP_TEXT = "Drag to rotate | Middle drag to pan | Scroll to zoom"


def _vec_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vec_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_scale(v, s):
    return (v[0] * s, v[1] * s, v[2] * s)


def _vec_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vec_length(v):
    return math.sqrt(_vec_dot(v, v))


def _vec_normalize(v):
    length = _vec_length(v)
    if length <= 1e-8:
        return (0.0, 1.0, 0.0)
    inv = 1.0 / length
    return (v[0] * inv, v[1] * inv, v[2] * inv)


def _face_normal(v0, v1, v2):
    return _vec_normalize(_vec_cross(_vec_sub(v1, v0), _vec_sub(v2, v0)))


def _as_gl_float_buffer(values):
    """Return a ctypes float buffer compatible with PyOpenGL uniform uploads."""
    return (ctypes.c_float * len(values))(*[float(v) for v in values])


class _SoftwareMeshViewer(QWidget):
    """CPU fallback mesh viewer with a cheaper interactive rendering path."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vertices = []
        self._normals = []
        self._faces = []
        self._face_normals = []
        self._face_colors: list[tuple[int, int, int, int]] = []
        self._rot_x = -25.0
        self._rot_y = 35.0
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._last_mouse = None
        self._center = (0.0, 0.0, 0.0)
        self._scale = 1.0
        self._info_text = ""
        self._interactive = False
        self.setMinimumSize(200, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def set_mesh(self, vertices, faces, normals=None, info_text="",
                 face_colors=None, texture_payload=None):
        # texture_payload is accepted for API parity with the GPU viewer
        # but the software path can't do per-pixel texture sampling at
        # interactive rates in pure Python. We ignore it and fall back
        # to the face_colors flat-shaded colour per face.
        del texture_payload
        self._vertices = list(vertices)
        if normals and len(normals) == len(self._vertices):
            self._normals = [tuple(n) for n in normals]
        else:
            self._normals = []
        self._faces = list(faces)
        self._info_text = info_text
        self._pan = QPointF(0.0, 0.0)
        self._zoom = 1.0
        self._interactive = False
        # Per-face RGBA tuples sampled from the mesh's diffuse texture by
        # core.mesh_texture_service. When supplied, they replace the
        # procedural blue-shift palette below so the preview matches the
        # in-game colours. Length must match self._faces; anything else
        # is discarded silently so callers can safely pass None.
        if face_colors and len(face_colors) == len(self._faces):
            self._face_colors = [tuple(c)[:4] for c in face_colors]
        else:
            self._face_colors = []

        if not self._vertices:
            self._face_normals = []
            self.update()
            return

        xs = [v[0] for v in self._vertices]
        ys = [v[1] for v in self._vertices]
        zs = [v[2] for v in self._vertices]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)
        self._center = (
            (min_x + max_x) / 2.0,
            (min_y + max_y) / 2.0,
            (min_z + max_z) / 2.0,
        )

        extent = max(max_x - min_x, max_y - min_y, max_z - min_z, 0.001)
        self._scale = 1.0 / extent

        self._face_normals = []
        for a, b, c in self._faces:
            if a < len(self._vertices) and b < len(self._vertices) and c < len(self._vertices):
                if self._normals:
                    avg = _vec_add(
                        _vec_add(self._normals[a], self._normals[b]),
                        self._normals[c],
                    )
                    self._face_normals.append(_vec_normalize(avg))
                else:
                    self._face_normals.append(_face_normal(
                        self._vertices[a], self._vertices[b], self._vertices[c]
                    ))
            else:
                self._face_normals.append((0.0, 1.0, 0.0))

        self.update()

    def clear(self):
        self._vertices = []
        self._normals = []
        self._faces = []
        self._face_normals = []
        self._face_colors = []
        self._info_text = ""
        self._interactive = False
        self.update()

    def _project_vertices(self):
        if not self._vertices:
            return []

        scale = self._scale * self._zoom * min(self.width(), self.height()) * 0.35
        ry = math.radians(self._rot_y)
        rx = math.radians(self._rot_x)
        cos_y = math.cos(ry)
        sin_y = math.sin(ry)
        cos_x = math.cos(rx)
        sin_x = math.sin(rx)
        cx = self.width() * 0.5 + self._pan.x()
        cy = self.height() * 0.5 + self._pan.y()

        out = []
        for vx, vy, vz in self._vertices:
            x = (vx - self._center[0]) * scale
            y = (vy - self._center[1]) * scale
            z = (vz - self._center[2]) * scale

            x2 = x * cos_y + z * sin_y
            z2 = -x * sin_y + z * cos_y
            y2 = y * cos_x - z2 * sin_x
            z3 = y * sin_x + z2 * cos_x

            out.append((cx + x2, cy - y2, z3))
        return out

    def paintEvent(self, event):
        painter = QPainter(self)
        interactive_fast = self._interactive and len(self._faces) > 4000
        painter.setRenderHint(QPainter.Antialiasing, not interactive_fast and len(self._faces) < 15000)
        painter.fillRect(self.rect(), QColor(24, 24, 37))

        if not self._vertices or not self._faces:
            painter.setPen(QColor(108, 112, 134))
            painter.drawText(self.rect(), Qt.AlignCenter, self._info_text or "No mesh loaded")
            painter.end()
            return

        projected = self._project_vertices()
        if not projected:
            painter.end()
            return

        light = _vec_normalize((0.3, 0.7, 0.5))

        if interactive_fast:
            step = max(1, len(self._faces) // 3500)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(120, 160, 220, 180), 0.6))
            for face_idx in range(0, len(self._faces), step):
                a, b, c = self._faces[face_idx]
                if a >= len(projected) or b >= len(projected) or c >= len(projected):
                    continue
                p0 = projected[a]
                p1 = projected[b]
                p2 = projected[c]
                area = abs((p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]))
                if area < 0.02:
                    continue
                painter.drawPolygon(QPolygonF([
                    QPointF(p0[0], p0[1]),
                    QPointF(p1[0], p1[1]),
                    QPointF(p2[0], p2[1]),
                ]))
        else:
            face_draws = []
            use_texture_colors = bool(self._face_colors)
            for face_idx, (a, b, c) in enumerate(self._faces):
                if a >= len(projected) or b >= len(projected) or c >= len(projected):
                    continue
                p0 = projected[a]
                p1 = projected[b]
                p2 = projected[c]
                area = abs((p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]))
                if area < 0.02:
                    continue

                normal = self._face_normals[face_idx] if face_idx < len(self._face_normals) else (0.0, 1.0, 0.0)
                dot = max(0.15, _vec_dot(normal, light))
                base_rgba: tuple[int, int, int, int] | None = None
                if use_texture_colors and face_idx < len(self._face_colors):
                    base_rgba = self._face_colors[face_idx]
                face_draws.append((
                    (p0[2] + p1[2] + p2[2]) / 3.0, p0, p1, p2, dot, base_rgba,
                ))

            face_draws.sort(key=lambda item: item[0])
            for _, p0, p1, p2, dot, base_rgba in face_draws:
                if base_rgba is not None:
                    # Diffuse-lit texture sample. Mix the sampled colour against
                    # the Lambert term so the mesh still reads as 3D instead of
                    # flat-shaded decals.
                    tr, tg, tb, ta = base_rgba
                    shade = 0.35 + 0.65 * dot  # 0.35 ambient, 0.65 diffuse
                    r = int(min(255, tr * shade))
                    g = int(min(255, tg * shade))
                    b = int(min(255, tb * shade))
                    alpha = ta if ta <= 255 else 255
                else:
                    # Procedural blue-shift palette — retained for meshes with
                    # no discovered texture so the preview still renders.
                    r = int(min(255, 80 + 100 * dot))
                    g = int(min(255, 120 + 80 * dot))
                    b = int(min(255, 180 + 60 * dot))
                    alpha = 220
                painter.setBrush(QBrush(QColor(r, g, b, alpha)))
                painter.setPen(QPen(QColor(40, 42, 54), 0.5))
                painter.drawPolygon(QPolygonF([
                    QPointF(p0[0], p0[1]),
                    QPointF(p1[0], p1[1]),
                    QPointF(p2[0], p2[1]),
                ]))

        painter.setPen(QColor(166, 173, 200))
        if self._info_text:
            painter.drawText(8, 16, self._info_text)
        painter.setPen(QColor(108, 112, 134))
        painter.drawText(8, self.height() - 8, _VIEWER_HELP_TEXT)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent):
        self._last_mouse = event.position()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._last_mouse is None:
            return

        dx = event.position().x() - self._last_mouse.x()
        dy = event.position().y() - self._last_mouse.y()
        self._last_mouse = event.position()

        if event.buttons() & Qt.LeftButton:
            self._interactive = True
            self._rot_y += dx * 0.5
            self._rot_x += dy * 0.5
            self._rot_x = max(-90.0, min(90.0, self._rot_x))
            self.update()
        elif event.buttons() & Qt.MiddleButton:
            self._interactive = True
            self._pan = QPointF(self._pan.x() + dx, self._pan.y() + dy)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._last_mouse = None
        if self._interactive:
            self._interactive = False
            self.update()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta > 0:
            self._zoom *= 1.15
        else:
            self._zoom /= 1.15
        self._zoom = max(0.1, min(20.0, self._zoom))
        self.update()


if _GL_RUNTIME_AVAILABLE:
    @dataclass
    class _TexturedBatch:
        """One contiguous range of vertices that share a single texture.

        ``texture_index`` is -1 when the range should render with the
        vertex-colour fallback (no diffuse available) — the shader
        switches on ``uUseTexture`` to cover both cases without a
        separate program.
        """
        first_vertex: int
        vertex_count: int
        texture_index: int


    @dataclass
    class _GpuTexture:
        """Decoded diffuse ready to upload as a GL texture object."""
        width: int
        height: int
        rgba: bytes


    @dataclass
    class _GpuMesh:
        positions: np.ndarray
        normals: np.ndarray
        # Per-vertex RGB in [0, 1]. In non-indexed mode this is filled
        # with neutral grey for untextured batches; textured batches set
        # it to (1, 1, 1) so the shader's Lambert math multiplies by the
        # sampled texture cleanly.
        colors: np.ndarray
        # Per-vertex UV. Non-textured batches get (0, 0) — the shader's
        # uUseTexture=0 path ignores this anyway.
        uvs: np.ndarray
        indices: np.ndarray
        center: tuple[float, float, float]
        radius: float
        # Ordered draw commands. Empty means the legacy indexed path
        # (single glDrawElements) should run.
        batches: list[_TexturedBatch]
        textures: list[_GpuTexture]


    class _OrbitCamera:
        def __init__(self):
            self.yaw = 0.0
            self.pitch = 0.3
            self.radius = 2.0
            self.target = np.zeros(3, dtype=np.float32)
            self.fov_y = 45.0
            self._last_x = 0.0
            self._last_y = 0.0

        def fit_to_sphere(self, center, radius):
            self.target = np.array(center, dtype=np.float32)
            half_fov = math.radians(self.fov_y * 0.5)
            self.radius = max(radius / max(math.sin(half_fov), 1e-6) * 1.3, 0.01)
            self.yaw = math.pi
            self.pitch = 0.3

        def eye_position(self):
            cp, sp = math.cos(self.pitch), math.sin(self.pitch)
            cy, sy = math.cos(self.yaw), math.sin(self.yaw)
            return self.target + self.radius * np.array([cp * sy, sp, cp * cy], dtype=np.float32)

        def view_matrix(self):
            eye = self.eye_position()
            forward = self.target - eye
            forward_len = float(np.linalg.norm(forward))
            if forward_len < 1e-8:
                return np.eye(4, dtype=np.float32)
            forward /= forward_len
            world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
            right = np.cross(forward, world_up)
            right_len = float(np.linalg.norm(right))
            if right_len < 1e-8:
                right = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            else:
                right /= right_len
            up = np.cross(right, forward)

            m = np.eye(4, dtype=np.float32)
            m[0, :3] = right
            m[1, :3] = up
            m[2, :3] = -forward
            m[0, 3] = -float(np.dot(right, eye))
            m[1, 3] = -float(np.dot(up, eye))
            m[2, 3] = float(np.dot(forward, eye))
            return m

        def proj_matrix(self, aspect):
            near = max(self.radius * 0.001, 0.001)
            far = self.radius * 100.0
            f = 1.0 / math.tan(math.radians(self.fov_y) * 0.5)
            m = np.zeros((4, 4), dtype=np.float32)
            m[0, 0] = f / max(aspect, 1e-6)
            m[1, 1] = f
            m[2, 2] = (far + near) / (near - far)
            m[2, 3] = (2.0 * far * near) / (near - far)
            m[3, 2] = -1.0
            return m

        def handle_press(self, x, y):
            self._last_x = x
            self._last_y = y

        def handle_move(self, buttons, x, y):
            dx = x - self._last_x
            dy = y - self._last_y
            self._last_x = x
            self._last_y = y

            if buttons & Qt.LeftButton:
                self.yaw -= dx * 0.005
                self.pitch += dy * 0.005
                self.pitch = max(-1.5, min(1.5, self.pitch))
            elif buttons & Qt.MiddleButton:
                cp = math.cos(self.pitch)
                sp = math.sin(self.pitch)
                cy = math.cos(self.yaw)
                sy = math.sin(self.yaw)
                right = (cy, 0.0, -sy)
                up = (-sp * sy, cp, -sp * cy)
                scale = self.radius * 0.002
                self.target = _vec_add(self.target, _vec_add(
                    _vec_scale(right, -dx * scale),
                    _vec_scale(up, dy * scale),
                ))

        def handle_scroll(self, delta):
            self.radius *= 0.9 ** (delta / 120.0)
            self.radius = max(0.01, self.radius)


    _VERT_SHADER = """#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
layout(location=2) in vec3 aColor;
layout(location=3) in vec2 aUV;
uniform mat4 uMVP;
out vec3 vNormal;
out vec3 vColor;
out vec2 vUV;
void main() {
    vNormal = aNormal;
    vColor = aColor;
    // The game's OBJ convention puts v=0 at the bottom, but we upload
    // the decoded DDS in DDS-native order (row 0 at the top). Flip here
    // so the shader reads the texture upright without a second CPU-side
    // pass over every UV.
    vUV = vec2(aUV.x, 1.0 - aUV.y);
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

    _FRAG_SHADER = """#version 330 core
in vec3 vNormal;
in vec3 vColor;
in vec2 vUV;
out vec4 FragColor;
uniform vec3 uLightDir;
uniform sampler2D uDiffuse;
// uUseTexture toggles between GPU-sampled per-pixel texturing (when the
// caller bound a real diffuse to texture unit 0) and the vertex-colour
// fallback path that keeps working for meshes with no discovered DDS.
uniform int uUseTexture;
void main() {
    vec3 baseColor = vColor;
    if (uUseTexture != 0) {
        baseColor = texture(uDiffuse, vUV).rgb;
    }
    vec3 N = normalize(vNormal);
    vec3 L = normalize(uLightDir);
    float diff = max(abs(dot(N, L)), 0.0);
    vec3 ambient = 0.25 * baseColor;
    vec3 diffuse = 0.75 * diff * baseColor;
    FragColor = vec4(ambient + diffuse, 1.0);
}
"""


    class _OpenGLMeshViewer(QOpenGLWidget):
        """Hardware accelerated mesh viewer."""

        def __init__(self, parent=None):
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            fmt.setSamples(4)
            fmt.setDepthBufferSize(24)
            super().__init__(parent)
            self.setFormat(fmt)
            self._vertices = []
            self._normals = []
            self._faces = []
            self._info_text = ""
            self._camera = _OrbitCamera()
            self._program = 0
            self._vao = 0
            self._vbo_pos = 0
            self._vbo_nor = 0
            self._vbo_col = 0
            self._vbo_uv = 0
            self._ebo = 0
            self._index_count = 0
            # True when we're rendering the compact indexed mesh
            # (glDrawElements with shared grey). False means we're
            # iterating the batch list with glDrawArrays ranges — the
            # textured path.
            self._indexed_draw = True
            self._gl_textures: list[int] = []   # GL texture object ids
            self._batches: list[_TexturedBatch] = []
            self._has_mesh = False
            self._gl_ready = False
            self._gl_error = ""
            self._pending_mesh = None
            self.setMinimumSize(200, 200)
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.StrongFocus)

        def set_mesh(self, vertices, faces, normals=None, info_text="",
                     face_colors=None, texture_payload=None):
            """Upload a mesh for rendering.

            ``texture_payload`` (core.mesh_texture_service.GpuTexturePayload)
            is the preferred input when the caller has resolved per-face
            texture data: it carries flattened positions / normals / UVs
            and a list of decoded DDS textures. Each triangle's texture
            id picks which texture sampler renders it, giving true per-
            pixel texturing with UV interpolation.

            ``face_colors`` (list of per-face RGBA) is the older
            per-face-flat fallback, used when texture_payload isn't
            available (software viewer hand-off, tests, etc.). Both
            kwargs exist so the preview-pane can degrade gracefully.
            """
            self._vertices = list(vertices)
            if normals and len(normals) == len(self._vertices):
                self._normals = [tuple(n) for n in normals]
            else:
                self._normals = []
            self._faces = list(faces)
            self._info_text = info_text

            if not self._vertices or not self._faces:
                self.clear()
                return

            self._pending_mesh = self._build_gpu_mesh(
                self._vertices, self._faces, self._normals,
                face_colors, texture_payload,
            )
            if self._gl_ready and self.context():
                self._upload_mesh(self._pending_mesh)
            self.update()

        def clear(self):
            self._has_mesh = False
            self._pending_mesh = None
            self._vertices = []
            self._normals = []
            self._faces = []
            self.update()

        def initializeGL(self):
            try:
                glEnable(GL_DEPTH_TEST)
                glEnable(GL_MULTISAMPLE)
                glClearColor(0.10, 0.10, 0.18, 1.0)
                self._compile_shaders()
                self._setup_buffers()
                self._gl_ready = True
                if self._pending_mesh is not None:
                    self._upload_mesh(self._pending_mesh)
            except Exception as exc:
                self._gl_error = str(exc)
                self._gl_ready = False
                self._has_mesh = False

        def resizeGL(self, width, height):
            glViewport(0, 0, width, height)

        def paintGL(self):
            glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
            if not (self._gl_ready and self._has_mesh):
                return
            try:
                aspect = self.width() / max(self.height(), 1)
                mvp = self._camera.proj_matrix(aspect) @ self._camera.view_matrix()
                glUseProgram(self._program)
                glUniformMatrix4fv(
                    glGetUniformLocation(self._program, "uMVP"),
                    1,
                    GL_TRUE,
                    mvp.astype(np.float32),
                )
                light = np.array([0.6, 0.8, 0.5], dtype=np.float32)
                light /= np.linalg.norm(light)
                glUniform3fv(
                    glGetUniformLocation(self._program, "uLightDir"),
                    1,
                    light,
                )

                diffuse_loc = glGetUniformLocation(self._program, "uDiffuse")
                use_tex_loc = glGetUniformLocation(self._program, "uUseTexture")
                glUniform1i(diffuse_loc, 0)  # sampler on texture unit 0

                glBindVertexArray(self._vao)

                if self._batches:
                    # GPU-textured path. One draw call per texture batch.
                    for batch in self._batches:
                        if batch.texture_index >= 0 and batch.texture_index < len(self._gl_textures):
                            glUniform1i(use_tex_loc, 1)
                            glActiveTexture(GL_TEXTURE0)
                            glBindTexture(GL_TEXTURE_2D, self._gl_textures[batch.texture_index])
                        else:
                            glUniform1i(use_tex_loc, 0)
                        glDrawArrays(GL_TRIANGLES, batch.first_vertex, batch.vertex_count)
                elif self._indexed_draw:
                    glUniform1i(use_tex_loc, 0)
                    glDrawElements(GL_TRIANGLES, self._index_count, GL_UNSIGNED_INT, None)
                else:
                    # Per-face-colour fallback — no textures available.
                    glUniform1i(use_tex_loc, 0)
                    glDrawArrays(GL_TRIANGLES, 0, self._index_count)
                glBindVertexArray(0)
            except Exception as exc:
                self._gl_error = str(exc)
                self._gl_ready = False

        def mousePressEvent(self, event: QMouseEvent):
            self._camera.handle_press(event.position().x(), event.position().y())

        def mouseMoveEvent(self, event: QMouseEvent):
            self._camera.handle_move(event.buttons(), event.position().x(), event.position().y())
            self.update()

        def wheelEvent(self, event: QWheelEvent):
            self._camera.handle_scroll(event.angleDelta().y())
            self.update()

        def _build_gpu_mesh(
            self, vertices, faces, normals=None, face_colors=None, texture_payload=None,
        ) -> _GpuMesh:
            if normals and len(normals) == len(vertices):
                vertex_normals = [list(_vec_normalize(tuple(n))) for n in normals]
            else:
                vertex_normals = [[0.0, 0.0, 0.0] for _ in vertices]
                for a, b, c in faces:
                    if a >= len(vertices) or b >= len(vertices) or c >= len(vertices):
                        continue
                    normal = _face_normal(vertices[a], vertices[b], vertices[c])
                    for idx in (a, b, c):
                        vertex_normals[idx][0] += normal[0]
                        vertex_normals[idx][1] += normal[1]
                        vertex_normals[idx][2] += normal[2]

            use_payload = texture_payload is not None and not texture_payload.is_empty
            use_per_face_color = bool(face_colors) and len(face_colors) == len(faces)
            default_color = (0.72, 0.72, 0.76)

            bbox_source = (
                texture_payload.positions if use_payload else vertices
            )
            if bbox_source:
                min_x = min(v[0] for v in bbox_source)
                max_x = max(v[0] for v in bbox_source)
                min_y = min(v[1] for v in bbox_source)
                max_y = max(v[1] for v in bbox_source)
                min_z = min(v[2] for v in bbox_source)
                max_z = max(v[2] for v in bbox_source)
                # Match CDMB's viewer fit so near/far clipping behaves the same on skewed meshes.
                center = (
                    (min_x + max_x) * 0.5,
                    (min_y + max_y) * 0.5,
                    (min_z + max_z) * 0.5,
                )
                radius = max((_vec_length(_vec_sub(v, center)) for v in bbox_source), default=0.01)
            else:
                center = (0.0, 0.0, 0.0)
                radius = 0.01

            if use_payload:
                # GPU-textured path. Group triangles by texture_index so
                # paintGL can bind each texture once and emit one draw
                # call per batch. Batches for texture_index == -1 render
                # with the vertex-colour fallback (shader's uUseTexture
                # uniform switches modes per batch).
                positions: list[tuple[float, float, float]] = []
                packed_normals: list[tuple[float, float, float]] = []
                uv_stream: list[tuple[float, float]] = []
                vertex_colors: list[tuple[float, float, float]] = []
                batches: list[_TexturedBatch] = []

                # Sort triangles by texture_index so same-texture ranges
                # are contiguous in the vertex buffer — one draw call each.
                tri_order = sorted(
                    range(texture_payload.triangle_count),
                    key=lambda ti: texture_payload.texture_ids[ti],
                )

                current_tex = None
                batch_start = 0
                for tri_idx in tri_order:
                    tex_id = texture_payload.texture_ids[tri_idx]
                    if tex_id != current_tex:
                        if current_tex is not None:
                            count = len(positions) - batch_start
                            if count > 0:
                                batches.append(_TexturedBatch(
                                    first_vertex=batch_start,
                                    vertex_count=count,
                                    texture_index=current_tex,
                                ))
                        current_tex = tex_id
                        batch_start = len(positions)

                    base = tri_idx * 3
                    for corner in range(3):
                        v = texture_payload.positions[base + corner]
                        n = texture_payload.normals[base + corner]
                        uv = texture_payload.uvs[base + corner]
                        positions.append((float(v[0]), float(v[1]), float(v[2])))
                        normal = _vec_normalize(tuple(n))
                        packed_normals.append((float(normal[0]), float(normal[1]), float(normal[2])))
                        uv_stream.append((float(uv[0]), float(uv[1])))
                        # Textured verts get white so Lambert(baseColor)
                        # in the shader equals Lambert(sampledTexel).
                        # Untextured verts carry the fallback grey.
                        if tex_id < 0:
                            vertex_colors.append(default_color)
                        else:
                            vertex_colors.append((1.0, 1.0, 1.0))
                if current_tex is not None:
                    count = len(positions) - batch_start
                    if count > 0:
                        batches.append(_TexturedBatch(
                            first_vertex=batch_start,
                            vertex_count=count,
                            texture_index=current_tex,
                        ))

                gpu_textures = [
                    _GpuTexture(width=t.width, height=t.height, rgba=t.rgba)
                    for t in texture_payload.textures
                ]

                return _GpuMesh(
                    positions=np.array(positions, dtype=np.float32),
                    normals=np.array(packed_normals, dtype=np.float32),
                    colors=np.array(vertex_colors, dtype=np.float32),
                    uvs=np.array(uv_stream, dtype=np.float32),
                    indices=np.array([], dtype=np.uint32),
                    center=center,
                    radius=max(radius, 0.01),
                    batches=batches,
                    textures=gpu_textures,
                )

            if use_per_face_color:
                # Per-face-colour mode: expand to non-indexed triangles so
                # each face can carry its own RGB without bleeding into
                # neighbours across shared vertices. Triples storage of
                # vertex attributes, but character-preview meshes are
                # measured in thousands of triangles — trivial for the GPU.
                positions = []
                packed_normals = []
                vertex_colors = []
                uv_stream = []

                for face_idx, face in enumerate(faces):
                    a, b, c = face
                    if a >= len(vertices) or b >= len(vertices) or c >= len(vertices):
                        continue
                    rgba = face_colors[face_idx]
                    if rgba is None or len(rgba) < 3:
                        color_rgb = default_color
                    else:
                        r, g, b_ = rgba[0], rgba[1], rgba[2]
                        color_rgb = (r / 255.0, g / 255.0, b_ / 255.0)
                    for idx in (a, b, c):
                        vertex = vertices[idx]
                        positions.append((float(vertex[0]), float(vertex[1]), float(vertex[2])))
                        normal = _vec_normalize(tuple(vertex_normals[idx]))
                        packed_normals.append((float(normal[0]), float(normal[1]), float(normal[2])))
                        vertex_colors.append((float(color_rgb[0]), float(color_rgb[1]), float(color_rgb[2])))
                        uv_stream.append((0.0, 0.0))

                return _GpuMesh(
                    positions=np.array(positions, dtype=np.float32),
                    normals=np.array(packed_normals, dtype=np.float32),
                    colors=np.array(vertex_colors, dtype=np.float32),
                    uvs=np.array(uv_stream, dtype=np.float32),
                    indices=np.array([], dtype=np.uint32),
                    center=center,
                    radius=max(radius, 0.01),
                    batches=[],
                    textures=[],
                )

            # Indexed path: one vertex per mesh vertex, all the same
            # default grey. Compact and matches the existing behaviour for
            # meshes where no texture could be resolved.
            positions = []
            packed_normals = []
            vertex_colors = []
            uv_stream = []
            dc = tuple(default_color)
            for idx, vertex in enumerate(vertices):
                positions.append((float(vertex[0]), float(vertex[1]), float(vertex[2])))
                normal = _vec_normalize(tuple(vertex_normals[idx]))
                packed_normals.append((float(normal[0]), float(normal[1]), float(normal[2])))
                vertex_colors.append(dc)
                uv_stream.append((0.0, 0.0))

            indices = []
            for a, b, c in faces:
                if a < len(vertices) and b < len(vertices) and c < len(vertices):
                    indices.extend((a, b, c))

            return _GpuMesh(
                positions=np.array(positions, dtype=np.float32),
                normals=np.array(packed_normals, dtype=np.float32),
                colors=np.array(vertex_colors, dtype=np.float32),
                uvs=np.array(uv_stream, dtype=np.float32),
                indices=np.array(indices, dtype=np.uint32),
                center=center,
                radius=max(radius, 0.01),
                batches=[],
                textures=[],
            )

        def _upload_mesh(self, mesh: _GpuMesh):
            if not self._gl_ready or mesh is None:
                return

            self.makeCurrent()
            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_pos)
            glBufferData(GL_ARRAY_BUFFER, mesh.positions.nbytes, mesh.positions.tobytes(), GL_STATIC_DRAW)
            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_nor)
            glBufferData(GL_ARRAY_BUFFER, mesh.normals.nbytes, mesh.normals.tobytes(), GL_STATIC_DRAW)
            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_col)
            glBufferData(GL_ARRAY_BUFFER, mesh.colors.nbytes, mesh.colors.tobytes(), GL_STATIC_DRAW)
            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_uv)
            glBufferData(GL_ARRAY_BUFFER, mesh.uvs.nbytes, mesh.uvs.tobytes(), GL_STATIC_DRAW)

            # Empty indices == non-indexed mode (per-face colour expansion
            # or textured batches).
            self._indexed_draw = mesh.indices.size > 0
            if self._indexed_draw:
                glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self._ebo)
                glBufferData(GL_ELEMENT_ARRAY_BUFFER, mesh.indices.nbytes, mesh.indices.tobytes(), GL_STATIC_DRAW)
                self._index_count = len(mesh.indices)
            else:
                # glDrawArrays count comes from the position VBO's vertex count.
                self._index_count = len(mesh.positions)

            # Free any old texture objects before uploading the new ones.
            if self._gl_textures:
                glDeleteTextures(len(self._gl_textures), self._gl_textures)
                self._gl_textures = []

            for gpu_tex in mesh.textures:
                tex_id = glGenTextures(1)
                glBindTexture(GL_TEXTURE_2D, tex_id)
                glTexImage2D(
                    GL_TEXTURE_2D, 0, GL_RGBA8,
                    gpu_tex.width, gpu_tex.height,
                    0, GL_RGBA, GL_UNSIGNED_BYTE, gpu_tex.rgba,
                )
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                glGenerateMipmap(GL_TEXTURE_2D)
                self._gl_textures.append(int(tex_id) if hasattr(tex_id, "__int__") else tex_id)
            glBindTexture(GL_TEXTURE_2D, 0)

            self._batches = list(mesh.batches)
            self._has_mesh = True
            self._camera.fit_to_sphere(mesh.center, mesh.radius)
            self.doneCurrent()

        def _compile_shaders(self):
            vertex_shader = glCreateShader(GL_VERTEX_SHADER)
            glShaderSource(vertex_shader, _VERT_SHADER)
            glCompileShader(vertex_shader)
            if not glGetShaderiv(vertex_shader, GL_COMPILE_STATUS):
                raise RuntimeError(glGetShaderInfoLog(vertex_shader).decode(errors="replace"))

            fragment_shader = glCreateShader(GL_FRAGMENT_SHADER)
            glShaderSource(fragment_shader, _FRAG_SHADER)
            glCompileShader(fragment_shader)
            if not glGetShaderiv(fragment_shader, GL_COMPILE_STATUS):
                raise RuntimeError(glGetShaderInfoLog(fragment_shader).decode(errors="replace"))

            self._program = glCreateProgram()
            glAttachShader(self._program, vertex_shader)
            glAttachShader(self._program, fragment_shader)
            glLinkProgram(self._program)
            if not glGetProgramiv(self._program, GL_LINK_STATUS):
                raise RuntimeError(glGetProgramInfoLog(self._program).decode(errors="replace"))

            glDeleteShader(vertex_shader)
            glDeleteShader(fragment_shader)

        def _setup_buffers(self):
            self._vao = glGenVertexArrays(1)
            self._vbo_pos, self._vbo_nor, self._vbo_col, self._vbo_uv = glGenBuffers(4)
            self._ebo = glGenBuffers(1)

            glBindVertexArray(self._vao)

            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_pos)
            glBufferData(GL_ARRAY_BUFFER, 0, None, GL_STATIC_DRAW)
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 12, None)
            glEnableVertexAttribArray(0)

            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_nor)
            glBufferData(GL_ARRAY_BUFFER, 0, None, GL_STATIC_DRAW)
            glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 12, None)
            glEnableVertexAttribArray(1)

            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_col)
            glBufferData(GL_ARRAY_BUFFER, 0, None, GL_STATIC_DRAW)
            glVertexAttribPointer(2, 3, GL_FLOAT, GL_FALSE, 12, None)
            glEnableVertexAttribArray(2)

            glBindBuffer(GL_ARRAY_BUFFER, self._vbo_uv)
            glBufferData(GL_ARRAY_BUFFER, 0, None, GL_STATIC_DRAW)
            glVertexAttribPointer(3, 2, GL_FLOAT, GL_FALSE, 8, None)
            glEnableVertexAttribArray(3)

            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self._ebo)
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, 0, None, GL_STATIC_DRAW)
            glBindVertexArray(0)


    MeshViewer = _OpenGLMeshViewer
else:
    MeshViewer = _SoftwareMeshViewer
