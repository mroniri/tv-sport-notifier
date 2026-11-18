# TV sport obaveštenja (fudbal)

Program jednom dnevno (i na svakih 6h) čita TV raspored sa naslovi.net, izdvaja
fudbalske utakmice po tvojim kriterijumima i ~1h pre početka šalje
notifikaciju na telefon preko [ntfy](https://ntfy.sh).

## Kriterijumi (izmena u `config.json`)

Isključivo muški fudbal (naslov mora da počinje sa „Fudbal“; futsal, mali fudbal i
američki fudbal ne prolaze), samo prave utakmice „A - B“ (pregledi, magazini,
klasici i reprize se odbacuju):

| Pravilo | Šta prolazi |
|---|---|
| Liga šampiona | sve utakmice (bez kvalifikacija) |
| Liga nacija | sve utakmice |
| Prvenstva | svetsko i evropsko prvenstvo (bez klupskog SP) |
| Kvalifikacije | za SP i EP, ali samo ako igra bar jedna od reprezentacija iz `rules.qualifiers.teams` (Nemačka, Italija, Španija, Francuska, Engleska, Holandija, Portugal, Belgija, Hrvatska) |
| Srbija | svaka utakmica u kojoj igra reprezentacija Srbije, uključujući kvalifikacije (i mlade selekcije, `include_serbia_youth`) |
| Derbiji | Engleska, Španija, Italija, Srbija — liste parova su u `derbies` |

Ženski fudbal se isključuje (`exclude_women`): naslovi sa „(Ž)“, „žene“, „women“.

Ostalo u `config.json`:

- `allowed_channels` – kanali iz tvog paketa SBB Net TV Premium + (sportski kanali, RTS 1-3,
  Prva, Pink, B92, Happy TV, Nova S, M4 Sport). Poređenje je tolerantno: „Arena sport
  Premium 1“ i „Arena Premium 1“, kao i „Eurosport 1“ i „Eurosport 1 HD“ su isti kanal.
  Sport Klub nije u tvom paketu, pa nije na listi. Prazna lista = svi kanali.
  `python tv_notifier.py --list` ispod liste prikazuje i utakmice koje su izbačene samo
  zbog kanala, pa lako vidiš šta nedostaje.
- `lead_minutes` – koliko ranije obaveštavati (60).
- `urls` – linkovi sa rasporedom; može ih biti više.

## Reprize

Program ih odbacuje na tri načina:

1. oznake u naslovu („(R)“, „repriza“, „snimak“, godina sezone i sl.);
2. ista utakmica više puta u istom danu: obaveštava se samo o prvom terminu;
3. utakmica koja je već bila u rasporedu prethodna 3 dana (`rerun_memory_days`) smatra se
   reprizom. Zato je prvi dan rada najmanje pouzdan: ujutru prikazana repriza sinoćnje
   utakmice biće tada još uzeta kao prava.

Ako se desi da propusti reprizu ili odbaci pravu utakmicu, pošalji mi naslov pa ću da dotegnem pravila.

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
