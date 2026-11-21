// Publica el veredicto de Cachai en el PR: un solo comentario que se edita en cada push
// y un check run con el resumen corto. Lo carga actions/github-script desde action.yml;
// action/publish.test.js lo prueba con un `github` falso (`node --test`).
'use strict';

const fs = require('fs');

const MARKER = '<!-- cachai-report -->';
const TITLE = '### Cachai:';
const MAX_BODY = 65536; // GitHub responde 422 por encima de esto
const CHECK_NAME = 'cachai: veredicto';

// Comentario propio: lo escribió el autor esperado, empieza con el título y termina con el marcador.
// Un comentario de otro con el marcador no cuenta: si no, su autor podría editar el veredicto.
function isOwn(comment, author) {
  const body = (comment.body || '').trimEnd();
  return Boolean(comment.user && comment.user.login === author && body.startsWith(TITLE) && body.endsWith(MARKER));
}

function minimalBody(sha, why) {
  return `${TITLE} SIN VEREDICTO publicable para \`${sha}\`\n\n${why}\n\n${MARKER}\n`;
}

async function findOwn(github, params, author) {
  let found = null;
  await github.paginate(github.rest.issues.listComments, { ...params, per_page: 100 }, (response, done) => {
    found = found || response.data.find((c) => isOwn(c, author)) || null;
    if (found) done();
    return [];
  });
  return found;
}

async function publish({ github, context, core, env, readFile = (p) => fs.readFileSync(p, 'utf8') }) {
  const pr = context.payload.pull_request;
  const { owner, repo } = context.repo;
  const sha = pr.head.sha;
  const author = env.AUTHOR || 'github-actions[bot]';
  const warn = async (what, why) => {
    core.warning(`Cachai no pudo ${what}: ${why}. El veredicto está en el resumen del job y en el artefacto cachai-report.`);
    await core.summary.addRaw(`\n> Cachai no pudo ${what}: ${why}.\n`).write();
  };

  // Sin el repo de origen (fork borrado) no se sabe si es un fork: se trata como tal.
  if (!pr.head.repo || pr.head.repo.full_name !== `${owner}/${repo}`) {
    await warn('comentar ni crear el check run', 'el PR viene de un fork y el token es de solo lectura');
    return { skipped: 'fork' };
  }

  let report = null;
  try {
    report = JSON.parse(readFile(env.REPORT));
  } catch (e) {
    report = null;
  }
  // Un reporte sin la parte pública (v1, o escrito a medias) se trata como ausente:
  // así el comentario anterior pasa a «sin veredicto» en vez de quedar mostrando el veredicto viejo.
  const pub = report && report.public;
  if (!pub || typeof pub.markdown !== 'string' || typeof pub.summary !== 'string' || typeof report.outcome !== 'string') {
    report = null;
  }
  const params = { owner, repo, issue_number: pr.number };
  const result = { comment: null, check: null };

  if (env.COMMENT === 'true') {
    try {
      const own = await findOwn(github, params, author);
      if (!report) {
        // La revisión se cayó o se canceló: el comentario anterior no puede seguir diciendo APROBADO.
        if (own) {
          await github.rest.issues.updateComment({ owner, repo, comment_id: own.id,
            body: `${TITLE} SIN VEREDICTO\n\nSin veredicto para \`${sha}\`: la revisión no terminó o se canceló. Ver el log del job.\n\n${MARKER}\n` });
          result.comment = 'stale';
        }
      } else {
        const write = (body) => own
          ? github.rest.issues.updateComment({ owner, repo, comment_id: own.id, body })
          : github.rest.issues.createComment({ ...params, body });
        const body = report.public.markdown;
        try {
          if (body.length > MAX_BODY) throw Object.assign(new Error('cuerpo de más de 65536 caracteres'), { status: 422 });
          await write(body);
          result.comment = own ? 'updated' : 'created';
        } catch (e) {
          if (e.status === 403) throw e;
          // Nunca dejar el comentario anterior: con un cuerpo mínimo, el PR no muestra un veredicto viejo.
          await write(minimalBody(sha, `El comentario completo no se pudo publicar (${e.status || ''} ${e.message}). Ver el resumen del job y el artefacto cachai-report.`));
          result.comment = 'minimal';
        }
      }
    } catch (e) {
      await warn('publicar el comentario', `${e.status || ''} ${e.message} (¿falta pull-requests: write?)`);
      result.comment = 'failed';
    }
  }

  if (env.CHECK_RUN === 'true' && report) {
    try {
      await github.rest.checks.create({
        owner, repo, name: CHECK_NAME, head_sha: sha, status: 'completed',
        conclusion: report.outcome === 'approved' ? 'success' : 'failure',
        output: { title: report.public.summary.slice(0, 255), summary: report.public.markdown.slice(0, MAX_BODY - 1) },
      });
      result.check = report.outcome === 'approved' ? 'success' : 'failure';
    } catch (e) {
      await warn('crear el check run', `${e.status || ''} ${e.message} (¿falta checks: write?)`);
      result.check = 'failed';
    }
  }
  return result;
}

module.exports = { publish, isOwn, minimalBody, MARKER, MAX_BODY };
