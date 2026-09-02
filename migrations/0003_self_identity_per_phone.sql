-- The WhatsApp phone number is the stable identity for the requester's own
-- ("self") patient record — name is an editable attribute, not part of the
-- identity, so it can never fragment into multiple self-records again.
--
-- Family members keep name+relation as their identity, now correctly
-- scoped by relation too (the old index ignored relation entirely, so two
-- different family members who happened to share a name would collide).
--
-- Run migrations/0001 and 0002 first if not already applied. Before running
-- this one, verify there are no existing conflicts:
--   SELECT hospital_id, requested_by_phone, COUNT(*)
--   FROM patients WHERE LOWER(relation_to_requester) = 'self'
--   GROUP BY hospital_id, requested_by_phone HAVING COUNT(*) > 1;
-- If that returns rows, resolve them (merge tokens onto the record you're
-- keeping, delete the duplicate) before this migration will succeed.

DROP INDEX IF EXISTS uq_patients_family_member;

CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_self_per_phone
ON patients (hospital_id, requested_by_phone)
WHERE LOWER(relation_to_requester) = 'self';

CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_family_member
ON patients (hospital_id, requested_by_phone, LOWER(name), LOWER(relation_to_requester))
WHERE LOWER(relation_to_requester) <> 'self';
