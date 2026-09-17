-- 목록 인덱스 정렬 방향을 쿼리(ORDER BY published_at DESC NULLS LAST)와 맞춘다.
-- CONCURRENTLY는 트랜잭션 블록 안에서 실행할 수 없으므로
-- Neon SQL Editor에서 한 문장씩 실행한다.

-- [0] 적용 전 실행 계획 기록 (Sort 단계가 있는지 확인해 결과를 저장해 둔다)
EXPLAIN ANALYZE
SELECT id, title FROM articles
WHERE category = '경제'
ORDER BY published_at DESC NULLS LAST
LIMIT 10;

-- [1] 새 인덱스 생성 (테이블 쓰기를 막지 않도록 CONCURRENTLY)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_category_published_nl
    ON articles (category, published_at DESC NULLS LAST);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_published_at_nl
    ON articles (published_at DESC NULLS LAST);

-- [2] 적용 후 실행 계획 기록 ([0]과 비교)
EXPLAIN ANALYZE
SELECT id, title FROM articles
WHERE category = '경제'
ORDER BY published_at DESC NULLS LAST
LIMIT 10;

-- [3] 새 인덱스가 쓰이는 것을 확인한 뒤 예전 인덱스 삭제
DROP INDEX CONCURRENTLY IF EXISTS idx_articles_category_published;
DROP INDEX CONCURRENTLY IF EXISTS idx_articles_published_at;
