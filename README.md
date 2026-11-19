# TV sport obaveštenja (fudbal)

Program na svakih 15 min proverava **zvanične rasporede takmičenja** (UEFA za Ligu
šampiona, ESPN za ostala takmičenja), izdvaja utakmice po tvojim kriterijumima i
~1h pre početka šalje notifikaciju preko [ntfy](https://ntfy.sh).

TV raspored (naslovi.net) služi samo da se vidi **na kom kanalu** se utakmica prenosi:
termin mora da bude u vreme početka prave utakmice. Utakmica koje nema u zvaničnom
rasporedu (reprize!) nema ni obaveštenja.

## Kriterijumi (izmena u `config.json`)

| Šta | Prolazi |
|---|---|
| Liga šampiona, Liga nacija, SP, EP | sve utakmice |
| Kvalifikacije za SP i EP | sve utakmice evropskih reprezentacija |
| Srbija | svaka utakmica reprezentacije Srbije (kvalifikacije, Liga nacija, prijateljske) |
| Derbiji | Engleska, Španija, Italija, Srbija — parovi u `derbies` |
| Vrh tabele | u istim ligama svaka utakmica između dva tima iz prva 4 mesta (`top_n` u `competitions`; tabela sa ESPN-a) |

- `competitions` – takmičenja i izvori (`uefa:ID` ili `espn:slug`), više izvora = rezerva.
- `require_tv_listing` – ako je `true`, obaveštava samo ako je utakmica na kanalu iz tvog paketa (`allowed_channels`).
- Ženski fudbal ne ulazi jer se prate samo muška takmičenja.

## Provera izvora

Prvo pokretanje: Actions → Run workflow sa kvačicom *refresh*. U repozitorijumu nastaje
`state/probe.txt` sa izveštajem koji izvori rade i koje utakmice vraćaju narednih 7 dana.
Ako je neki izvor `[GREŠKA]`, javi mi sadržaj tog fajla.

## Podešavanje (jednokratno)

1. **Telefon:** instaliraj aplikaciju **ntfy** (Android / iOS) i pretplati se na
   temu sa dugim, nasumičnim imenom, npr. `tv-sport-k7x2q9m4p1` (ko zna ime teme,
   može da ti šalje poruke, pa ga drži u tajnosti).
2. **GitHub:** napravi nalog i novi repozitorijum (preporuka: *public* — Actions je
   tada besplatan bez ograničenja; tema je u tajnom podešavanju, pa nije vidljiva).
   Otpremi sve fajlove iz ovog foldera.
3. **Tajna:** Settings → Secrets and variables → Actions → *New repository secret*:
   ime `NTFY_TOPIC`, vrednost = ime teme iz koraka 1.
4. **Uključi Actions:** kartica *Actions* → uključi workflow-e → *TV sport
   obaveštenja* → *Run workflow*. U logu proveri da piše koliko je termina
   pročitano.
5. Kada ti stigne prva notifikacija (ili proveriš da je log bez grešaka), gotovo je.
   Radi automatski svakih 15 minuta.

Ako stranica ne može da se pročita (promenjen izgled ili blokiran pristup),
dobićeš jednu notifikaciju o grešci dnevno.

## Lokalno testiranje

```bash
python tv_notifier.py --debug-parse        # šta je parser pročitao sa stranice
python tv_notifier.py --list               # današnje utakmice koje prolaze filtere
python tv_notifier.py --dry-run            # šta bi bilo poslato sada
NTFY_TOPIC=tvoja-tema python tv_notifier.py --test-notify
python -m unittest discover -s tests       # testovi
```

## Napomene

- GitHub Actions ne garantuje tačan minut: obaveštenja stižu otprilike 52–67 min
  pre početka (ponekad kasnije ako GitHub zakasni).
- Privatni repozitorijum ima 2000 besplatnih minuta mesečno; sa pokretanjem na
  svakih 15 min to je na granici. Za privatni repo u `notify.yml` stavi
  `*/30` umesto `*/15`.
- GitHub gasi zakazane workflow-e posle 60 dana bez aktivnosti u repozitorijumu;
  ako se to desi, ponovo ih uključi u kartici *Actions*.
